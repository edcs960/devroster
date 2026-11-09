"""Bounded read-only projections explain current gates and historical evidence."""

import json
import time
from dataclasses import asdict
from pathlib import Path

from agent_dev.adapters.secrets import registered_secrets
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.workspace import configuration_root
from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError, ErrorCode


class TaskQueries:
    def __init__(self, root: Path, db_path=None):
        from agent_dev.adapters.workflow_runtime import project_root

        self.root = configuration_root(root)
        from agent_dev.adapters.storage_registry import workspace_locations

        self.workspace = project_root(self.root)
        self.locations = workspace_locations(self.root)
        path = Path(db_path or self.locations.state_db)
        self.db = Database(path, read_only=True) if path.is_file() else None
        self.secrets = registered_secrets(self.root)

    @staticmethod
    def limit(value):
        if not isinstance(value, int) or not 1 <= value <= 200:
            raise DomainError(ErrorCode.INVALID_INPUT, "Query limit must be between 1 and 200.")
        return value

    def legacy_entries(self):
        from agent_dev.domain.legacy import parse_task_status

        path = self.root / "data/backlog.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"tasks": []}
        entries = []
        for item in data.get("tasks", []):
            original = item.get("status", "planned")
            try:
                state = parse_task_status(original).value
            except DomainError:
                state = original
            entries.append(
                dict(
                    item,
                    id=item.get("id", item.get("task_id", "")),
                    state=state,
                    legacy_state=original,
                )
            )
        return entries

    def tasks(self, limit=100, *, search="", state="", project="", page=1):
        from agent_dev.domain.status import TaskStatus

        limit = self.limit(limit)
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 1_000_000:
            raise DomainError(ErrorCode.INVALID_INPUT, "Page must be between 1 and 1000000.")
        if any(
            not isinstance(value, str) or len(value) > 200 for value in [search, state, project]
        ):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid task filters.")
        if state and state not in {item.value for item in TaskStatus}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown task state filter.")
        offset = (page - 1) * limit
        if self.db:
            clauses, parameters = [], []
            if search:
                escaped = search.replace("!", "!!").replace("%", "!%").replace("_", "!_")
                clauses.append("(id LIKE ? ESCAPE '!' OR title LIKE ? ESCAPE '!')")
                parameters.extend(["%" + escaped + "%"] * 2)
            for field, value in [("state", state), ("project_id", project)]:
                if value:
                    clauses.append(field + "=?")
                    parameters.append(value)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = self.db.rows(
                "SELECT id,project_id,title,state,version,priority FROM tasks"
                + where
                + " ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'blocked' THEN 1 WHEN 'waiting_for_capacity' THEN 2 ELSE 3 END,id LIMIT ? OFFSET ?",
                tuple(parameters + [limit, offset]),
            )
            total = self.db.rows("SELECT COUNT(*) AS n FROM tasks")[0]["n"]
            filtered = self.db.rows("SELECT COUNT(*) AS n FROM tasks" + where, tuple(parameters))[
                0
            ]["n"]
            counts = {
                row["state"]: row["n"]
                for row in self.db.rows("SELECT state,COUNT(*) AS n FROM tasks GROUP BY state")
            }
            projects = self.db.rows("SELECT id,name FROM projects ORDER BY id")
            source = "canonical"
        else:
            entries = [
                {
                    "id": item["id"],
                    "project_id": item.get("project_id", ""),
                    "title": item.get("title", ""),
                    "state": item["state"],
                    "legacy_state": item["legacy_state"],
                    "priority": item.get("priority", "medium"),
                }
                for item in self.legacy_entries()
            ]
            total = len(entries)
            counts = {}
            for item in entries:
                counts[item["state"]] = counts.get(item["state"], 0) + 1
            selected = [
                item
                for item in entries
                if (not state or item["state"] == state)
                and (not project or item["project_id"] == project)
                and (
                    not search or search.casefold() in (item["id"] + " " + item["title"]).casefold()
                )
            ]
            filtered = len(selected)
            rows = selected[offset : offset + limit]
            projects = [
                {"id": value, "name": value}
                for value in sorted({item["project_id"] for item in entries if item["project_id"]})
            ]
            source = "legacy"
        return redact(
            {
                "tasks": rows,
                "total": total,
                "filtered_total": filtered,
                "page": page,
                "pages": max(1, (filtered + limit - 1) // limit),
                "has_previous": page > 1,
                "has_next": offset + limit < filtered,
                "limit": limit,
                "truncated": filtered > limit,
                "source": source,
                "counts": counts,
                "projects": projects,
                "filters": {"search": search, "state": state, "project": project},
            },
            self.secrets,
        )

    def usage(self, task_id):
        from agent_dev.domain.execution_limits import limits

        self.show(task_id)
        assert self.db is not None
        repository = WorkflowRepository(self.db)
        task = repository.task(task_id)
        snapshot = repository.snapshot(task_id)["resolved"] if task.snapshot_id else {}
        return {
            "task_id": task_id,
            "limits": limits(snapshot.get("execution_limits")),
            "usage": repository.execution_usage(task_id),
            "limits_source": "approved_snapshot"
            if snapshot.get("execution_limits")
            else "bounded_compatibility_defaults",
            "cost": __import__("agent_dev.adapters.execution_view", fromlist=["cost"]).cost(self.db, task_id)
            if task.snapshot_id
            else None,
        }

    def show(self, task_id):
        if not self.db:
            raise DomainError(
                ErrorCode.INVALID_INPUT, "No canonical database; initialize or migrate first."
            )
        return redact(asdict(WorkflowRepository(self.db).task(task_id)), self.secrets)

    def explain(self, task_id, limit=100):
        limit = self.limit(limit)
        if not self.db:
            return self.explain_legacy(task_id, limit)
        task = self.show(task_id)
        assert self.db is not None
        reasons = []

        def reason(code, text, action):
            reasons.append({"code": code, "message": text, "action": action})

        plan = self.db.rows("SELECT * FROM task_plans WHERE id=?", (task["plan_id"],))
        approval = {"design": False, "plan": False, "snapshot": bool(task["snapshot_id"])}
        if plan:
            repository = WorkflowRepository(self.db)
            connection = self.db.connect()
            try:
                approval["design"] = repository._design_approved(
                    connection, plan[0]["design_id"], plan[0]["design_version"]
                )
            finally:
                connection.close()
            approval["plan"] = bool(
                self.db.rows(
                    "SELECT id FROM approvals WHERE target_type='plan' AND target_id=? AND target_version=? AND digest=? AND state='approved'",
                    (plan[0]["id"], plan[0]["version"], plan[0]["digest"]),
                )
            )
        terminal = task["state"] in {"completed", "cancelled"}
        if not terminal:
            if not approval["design"]:
                reason(
                    "DESIGN_APPROVAL_REQUIRED",
                    "Current design approval is missing or invalidated.",
                    "Review and approve the current design version.",
                )
            if not approval["plan"]:
                reason(
                    "PLAN_APPROVAL_REQUIRED",
                    "Plan approval is missing or invalidated.",
                    "Review the plan and run devroster plan approve PLAN-ID.",
                )
            if not approval["snapshot"]:
                reason(
                    "SNAPSHOT_MISSING",
                    "Task has no immutable input snapshot.",
                    "Approve the bound plan to prepare a snapshot and task worktree.",
                )
        dependencies = self.db.rows(
            "SELECT t.id,t.title,t.state FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_task_id WHERE d.task_id=?",
            (task_id,),
        )
        pending = [row["id"] for row in dependencies if row["state"] != "completed"]
        if pending and not terminal:
            reason(
                "DEPENDENCY_PENDING",
                "Dependencies are incomplete: " + ", ".join(pending),
                "Complete the dependency tasks before starting this task.",
            )
        path = self.root / "config/runtime.json"
        config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        flags = config.get("orchestrator_v2", {})
        if not flags.get("execute") and not terminal:
            reason(
                "EXECUTION_DISABLED",
                "Execution is disabled in this workspace.",
                "After configuration review, run devroster refactor configure execute.",
            )
        if task["state"] == "waiting_for_capacity":
            reason(
                "CAPACITY_WAIT",
                "Runner capacity or quota is unavailable.",
                f"After provider cooldown, run devroster task resume {task_id}.",
            )
        findings = self.db.rows(
            "SELECT id,fingerprint,severity,owner_phase,state,evidence,retries FROM findings WHERE task_id=? ORDER BY state,id LIMIT ?",
            (task_id, limit),
        )
        if any(row["state"] == "open" for row in findings):
            reason(
                "OPEN_FINDINGS",
                "Review findings require work or reentry.",
                "Inspect the owning phase and evidence below before resuming.",
            )
        attempts = self.db.rows(
            "SELECT id,phase_id,attempt_no,generation,state,started_at,ended_at FROM phase_attempts WHERE task_id=? ORDER BY started_at DESC,id DESC LIMIT ?",
            (task_id, limit),
        )
        verification = self.db.rows(
            "SELECT * FROM verification_runs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        gates = (
            self.db.rows(
                "SELECT gate,passed,required,exit_code,evidence FROM verification_results WHERE run_id=?",
                (verification[0]["id"],),
            )
            if verification
            else []
        )
        if verification and verification[0]["state"] == "failed":
            reason(
                "VERIFICATION_FAILED",
                "The latest required verification did not pass.",
                "Inspect gate evidence, implement the fix and re-run verification through the workflow.",
            )
        snapshots = self.db.rows(
            "SELECT digest,resolved_json FROM input_snapshots WHERE id=?", (task["snapshot_id"],)
        )
        snapshot = None
        if snapshots:
            resolved = json.loads(snapshots[0]["resolved_json"])
            relocated = any(
                not Path(binding["worktree"])
                .resolve()
                .is_relative_to(self.locations.worktree_root)
                for binding in resolved.get("repositories", {}).values()
            )
            if relocated and not terminal:
                reason(
                    "SNAPSHOT_RELOCATION",
                    "This historical snapshot refers to worktrees in another workspace.",
                    "Review the destination repository profile and create newly approved plans/tasks; do not resume historical snapshots.",
                )
            phases = resolved.get("phases", [])
            snapshot = {
                "digest": snapshots[0]["digest"],
                "phases": [phase["id"] for phase in phases],
                "repositories": {
                    name: {"branch": binding.get("branch"), "worktree": binding.get("worktree")}
                    for name, binding in resolved.get("repositories", {}).items()
                },
            }
            eligible = [
                runner
                for runner in resolved.get("runners", [])
                if runner.get("enabled") is True and runner.get("authenticated") is True
            ]
            if not eligible and not terminal:
                reason(
                    "RUNNER_CONFIGURATION",
                    "The approved snapshot has no enabled authenticated runner.",
                    "Review the runner profile and create a newly approved task; snapshots are immutable.",
                )
        events = self.db.rows(
            "SELECT version,kind,payload_json,created_at FROM events WHERE aggregate_id=? ORDER BY version DESC LIMIT ?",
            (task_id, limit),
        )
        events = [dict(row, payload=json.loads(row["payload_json"])) for row in events]
        for row in events:
            del row["payload_json"]
        if task["state"] in {"blocked", "waiting_for_capacity"}:
            # Only the newest attempt's failure describes the current interruption.
            latest = attempts[0] if attempts else None
            failure = next(
                (
                    row
                    for row in events
                    if row["kind"] == "ExecutionBlocked"
                    and (not latest or row["created_at"] >= latest["started_at"])
                ),
                None,
            )
            if failure and failure["payload"].get("reason") == "INTERRUPTED_CHANGES":
                reason(
                    "INTERRUPTED_CHANGES",
                    "A crashed attempt left changes its role was not allowed to make; they were copied aside.",
                    f"Inspect the copy under the task worktree folder (.attempts); resuming restores those paths and reruns the step: devroster task resume {task_id}.",
                )
            elif failure and failure["payload"].get("reason") == "BUDGET_PAUSE":
                reason(
                    "BUDGET_PAUSE",
                    "The estimated cost reached the budget; execution paused before the next AI call.",
                    f"Check usage (devroster task usage {task_id}); resuming approves one more budget: devroster task resume {task_id}.",
                )
            elif failure and failure["payload"].get("reason") == "AWAITING_WORKFLOW_APPROVAL":
                # A gate waiting for the user is a normal step, not a failure.
                reason(
                    "WORKFLOW_APPROVAL_PENDING",
                    "A workflow gate is waiting for approval.",
                    f"Approve or reject the gate on /workflows?task={task_id}, then resume the task.",
                )
            elif failure:
                payload = failure["payload"]
                reason(
                    payload.get("code", "TASK_BLOCKED"),
                    payload.get("reason", "Execution failed."),
                    "Inspect the latest failure evidence and resolve its cause before resuming.",
                )
        if task["state"] == "blocked" and not reasons:
            reason(
                "TASK_BLOCKED",
                "The workflow was blocked; inspect the latest event/attempt evidence.",
                f"After resolving the cause, run devroster task resume {task_id}.",
            )
        if task["state"] == "verifying":
            reason(
                "VERIFICATION_PENDING",
                "Phase execution finished; acceptance gates still need to run.",
                f"Run devroster task verify {task_id}.",
            )
        if task["state"] in {"pr_ready", "user_review"}:
            reason(
                "REVIEW_PENDING",
                "Verification passed; publication, CI/merge or explicit completion remains.",
                "Inspect the publication policy and review the resulting PR before completion.",
            )
        usage = self.usage(task_id)
        if task["state"] == "feedback_required":
            reason(
                "FEEDBACK_PENDING",
                "User feedback requires an approved amendment plan.",
                "Review feedback and approve a new plan before further execution.",
            )
        if task["state"] in {"running", "ready", "blocked", "waiting_for_capacity"} and (
            usage["usage"]["attempts"] >= usage["limits"]["max_attempts"]
            or usage["usage"]["runner_seconds"] >= usage["limits"]["max_runner_seconds"]
        ):
            reason(
                "EXECUTION_LIMIT_REACHED",
                "The approved attempt or runner-time budget is exhausted.",
                "Inspect usage and findings. Create a newly approved task for additional scope; resume does not reset the budget.",
            )
        next_actions = list(dict.fromkeys(row["action"] for row in reasons))
        if not next_actions:
            next_actions = [
                "No execution needed; terminal evidence remains available."
                if terminal
                else "Execution is already running."
                if task["state"] == "running"
                else f"Run devroster task run {task_id}."
            ]
        result = {
            "source": "canonical",
            "task": task,
            "approvals": approval,
            "reasons": reasons,
            "next_actions": next_actions,
            "dependencies": dependencies,
            "snapshot": snapshot,
            "attempts": attempts,
            # Workflow node states of the newest generation; skipped roles are not pending.
            "node_states": {
                row["node_id"]: row["state"]
                for row in self.db.rows(
                    "SELECT node_id,state FROM workflow_node_runs WHERE task_id=? AND generation="
                    "(SELECT MAX(generation) FROM workflow_node_runs WHERE task_id=?)",
                    (task_id, task_id),
                )
            }
            if self.db.rows("SELECT 1 FROM sqlite_master WHERE name='workflow_node_runs'")
            else {},
            "findings": findings,
            "verification": verification[0] if verification else None,
            "gates": gates,
            "timeline": list(reversed(events)),
            "history_limit": limit,
            "execution": usage,
            "updated_at": time.time(),
        }
        return redact(result, self.secrets)

    def explain_legacy(self, task_id, limit):
        entries = self.legacy_entries()
        item = next((row for row in entries if row["id"] == task_id), None)
        if item is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown task ID.")
        dependencies = [
            {"id": row["id"], "title": row.get("title", ""), "state": row["state"]}
            for row in entries
            if row["id"] in item.get("depends_on", [])
        ]
        reasons = [
            {
                "code": "LEGACY_RECORD",
                "message": "This task uses the existing workflow.",
                "action": "Inspect the existing checkpoint and use the existing workflow commands.",
            }
        ]
        if item["state"] not in {"completed", "cancelled"}:
            pending = [row["id"] for row in dependencies if row["state"] != "completed"]
            missing = [
                value
                for value in item.get("depends_on", [])
                if value not in {row["id"] for row in dependencies}
            ]
            if pending or missing:
                reasons.append(
                    {
                        "code": "DEPENDENCY_PENDING",
                        "message": ", ".join(pending + missing),
                        "action": "Complete or correct prerequisite tasks first.",
                    }
                )
        return redact(
            {
                "source": "legacy",
                "task": {
                    "task_id": task_id,
                    "state": item["state"],
                    "legacy_state": item["legacy_state"],
                    "definition": item,
                },
                "approvals": {"design": None, "plan": None, "snapshot": None},
                "reasons": reasons,
                "next_actions": [row["action"] for row in reasons],
                "dependencies": dependencies,
                "snapshot": None,
                "attempts": [],
                "findings": [],
                "verification": None,
                "gates": [],
                "timeline": [],
                "history_limit": limit,
                "execution": None,
                "updated_at": time.time(),
            },
            self.secrets,
        )
