"""Canonical workflow repository. State, events and notification intents commit together."""

import json
import time
import uuid
from dataclasses import replace
from typing import Any

from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.domain.content import canonical, digest, redact
from agent_dev.domain.contracts import TaskRecord
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext, transition_task


def identifier() -> str:
    return uuid.uuid4().hex


def validate_dag(tasks: list[dict], existing: set[str] | frozenset[str] = frozenset()) -> None:
    if not isinstance(tasks, list) or any(
        not isinstance(task, dict)
        or not isinstance(task.get("id"), str)
        or not task["id"]
        or not isinstance(task.get("title"), str)
        or not task["title"]
        or not isinstance(task.get("depends_on", []), list)
        or any(not isinstance(d, str) for d in task.get("depends_on", []))
        for task in tasks
    ):
        raise DomainError(
            ErrorCode.INVALID_INPUT, "Tasks require ID/title strings and a dependency list."
        )
    edges = {task["id"]: list(task.get("depends_on", [])) for task in tasks}
    if len(edges) != len(tasks):
        raise DomainError(ErrorCode.INVALID_INPUT, "Duplicate task ID.")
    visiting, visited = set(), set()

    def visit(task_id):
        if task_id in visiting:
            raise DomainError(ErrorCode.INVALID_INPUT, "Task dependency cycle.")
        if task_id in visited or task_id in existing:
            return
        if task_id not in edges:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown task dependency.")
        visiting.add(task_id)
        for dependency in edges[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in edges:
        visit(task_id)


class WorkflowRepository:
    def __init__(self, db: Database, *, clock=time.time, secrets: tuple[str, ...] = ()):
        self.db, self.clock, self.secrets = db, clock, secrets
        # Project hook observer (ai_hooks.HookObserver); None when the project has no hooks.
        self.hook_observer: Any = None

    def _json(self, content) -> str:
        return canonical(redact(content, self.secrets))

    def _event(self, connection, aggregate_id, kind, payload) -> str:
        version = connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM events WHERE aggregate_id=?", (aggregate_id,)
        ).fetchone()[0]
        event_id = identifier()
        payload_json = self._json(payload)
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (event_id, aggregate_id, version, kind, payload_json, self.clock()),
        )
        observer = self.hook_observer
        if observer is not None:
            observer.notify(aggregate_id, kind, payload)  # Queues only; hooks run elsewhere.
        project_kinds = {
            "ProjectRegistered",
            "ProfileRevised",
            "WorkspaceRestored",
            "DesignRevised",
            "ToolRegistered",
            "ToolCall",
            "ToolGrantIssued",
        }
        if kind in project_kinds and kind != "ToolGrantIssued":
            owner = connection.execute(
                "SELECT id FROM projects WHERE id=?", (aggregate_id,)
            ).fetchone()
        elif kind in {"PlanCreated", "PlanDraftCreated", "PlanTasksMaterialized"} or (
            kind == "ApprovalRecorded" and payload.get("target_type") == "plan"
        ):
            owner = connection.execute(
                "SELECT project_id FROM task_plans WHERE id=?", (aggregate_id,)
            ).fetchone()
        elif kind == "ApprovalRecorded":
            owner = connection.execute(
                "SELECT project_id FROM design_documents WHERE id=? LIMIT 1", (aggregate_id,)
            ).fetchone()
        elif kind == "FeedbackEvidenceRecorded":
            owner = connection.execute(
                "SELECT project_id FROM feedback_tasks WHERE id=?", (aggregate_id,)
            ).fetchone()
        else:
            owner = connection.execute(
                "SELECT project_id FROM tasks WHERE id=? UNION ALL SELECT id FROM projects WHERE id=? LIMIT 1",
                (aggregate_id, aggregate_id),
            ).fetchone()
        project_id = owner[0] if owner else ""
        connection.execute(
            "INSERT INTO notification_outbox(id,event_id,channel,payload_json,state,"
            "next_attempt_at) VALUES(?,?,?,?,?,?)",
            (
                identifier(),
                event_id,
                "workflow",
                self._json(
                    {
                        "event_id": event_id,
                        "aggregate_id": aggregate_id,
                        "project_id": project_id,
                        "kind": kind,
                        "payload": payload,
                    }
                ),
                "pending",
                self.clock(),
            ),
        )
        return event_id

    def event(self, aggregate_id: str, kind: str, payload: dict) -> str:
        with self.db.transaction() as connection:
            return self._event(connection, aggregate_id, kind, payload)

    def recover_attempts(self, task_id, lease):
        """Fence crashed attempts before budgets are checked, even when exhausted."""
        self._task_lease(task_id, lease)
        with self.db.transaction() as connection:
            self.assert_lease(connection, lease)
            rows = connection.execute(
                "SELECT * FROM phase_attempts WHERE task_id=? AND state='running' AND lease_token<>?",
                (task_id, lease["token"]),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE phase_attempts SET state='interrupted',ended_at=? WHERE id=?",
                    (self.clock(), row["id"]),
                )
                self._event(
                    connection,
                    task_id,
                    "PhaseInterrupted",
                    {"attempt_id": row["id"], "reason": "fenced_recovery"},
                )

    def execution_usage(self, task_id):
        from agent_dev.domain.execution_limits import summarize

        # The attempt budget counts AI calls: passes copied into a new generation on
        # re-entry never ran (no PhaseStarted), and session members share their session's one
        # call (ai_call false). Counting either spent 20 attempts after one re-entry of a
        # 12-role workflow.
        attempts = self.db.rows(
            "SELECT id,started_at,ended_at FROM phase_attempts WHERE task_id=? AND id IN "
            "(SELECT json_extract(payload_json,'$.attempt_id') FROM events "
            "WHERE aggregate_id=? AND kind='PhaseStarted') AND id NOT IN "
            "(SELECT json_extract(payload_json,'$.attempt_id') FROM events "
            "WHERE aggregate_id=? AND kind='RunnerUsage' "
            "AND json_extract(payload_json,'$.ai_call') = 0)",
            (task_id, task_id, task_id),
        )
        events = self.db.rows(
            "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='RunnerUsage' ORDER BY version",
            (task_id,),
        )
        return summarize(
            attempts, [json.loads(row["payload_json"]) for row in events], self.clock()
        )

    def register_project(self, project_id: str, name: str, profile: dict) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO projects VALUES(?,?,?,'draft',0,?)",
                (project_id, name, name, self.clock()),
            )
            safe = json.loads(self._json(profile))
            connection.execute(
                "INSERT INTO project_profile_versions VALUES(?,?,?,?)",
                (project_id, 1, digest(safe), canonical(safe)),
            )
            self._event(connection, project_id, "ProjectRegistered", {"profile_version": 1})

    def revise_profile(self, project_id: str, profile: dict, expected_version: int) -> int:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT version FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if row is None or row[0] != expected_version:
                raise DomainError(ErrorCode.STALE_VERSION, "Project version changed.")
            version = connection.execute(
                "SELECT MAX(version)+1 FROM project_profile_versions WHERE project_id=?",
                (project_id,),
            ).fetchone()[0]
            safe = json.loads(self._json(profile))
            connection.execute(
                "INSERT INTO project_profile_versions VALUES(?,?,?,?)",
                (project_id, version, digest(safe), canonical(safe)),
            )
            connection.execute("UPDATE projects SET version=version+1 WHERE id=?", (project_id,))
            self._event(connection, project_id, "ProfileRevised", {"profile_version": version})
            return version

    def design(self, project_id: str, design_id: str, content: str) -> int:
        content = redact(content, self.secrets)
        with self.db.transaction() as connection:
            owner = connection.execute(
                "SELECT project_id FROM design_documents WHERE id=? LIMIT 1", (design_id,)
            ).fetchone()
            if owner and owner[0] != project_id:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Design identifier belongs to another project."
                )
            version = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM design_documents WHERE id=?", (design_id,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO design_documents VALUES(?,?,?,?,?,'user_review')",
                (design_id, project_id, version, digest(content), content),
            )
            connection.execute(
                "UPDATE approvals SET state='invalidated' "
                "WHERE target_type='design' AND target_id=?",
                (design_id,),
            )
            self._event(
                connection, project_id, "DesignRevised", {"id": design_id, "version": version}
            )
            return version

    def approve(self, target_type: str, target_id: str, version: int, actor: str) -> str:
        if target_type not in {"design", "plan"} or not actor:
            raise DomainError(ErrorCode.INVALID_INPUT, "Approval target and actor required.")
        with self.db.transaction() as connection:
            if target_type == "design":
                row = connection.execute(
                    "SELECT * FROM design_documents WHERE id=? AND version=?", (target_id, version)
                ).fetchone()
                latest = connection.execute(
                    "SELECT MAX(version) FROM design_documents WHERE id=?", (target_id,)
                ).fetchone()[0]
                if row is None or version != latest:
                    raise DomainError(ErrorCode.STALE_VERSION, "Design version changed.")
            else:
                row = connection.execute(
                    "SELECT * FROM task_plans WHERE id=? AND version=?", (target_id, version)
                ).fetchone()
                if row is None or not self._design_approved(
                    connection, row["design_id"], row["design_version"]
                ):
                    raise DomainError(
                        ErrorCode.APPROVAL_REQUIRED, "Current design approval required."
                    )
            if target_type == "design":
                from agent_dev.application.design import DesignAssessment

                assessment = DesignAssessment().assess(row["content"])
                if not assessment.sufficient:
                    raise DomainError(ErrorCode.APPROVAL_REQUIRED, assessment.question)
            approval_id = identifier()
            connection.execute(
                "INSERT INTO approvals VALUES(?,?,?,?,?,?,'approved',?) "
                "ON CONFLICT(target_type,target_id,target_version,actor) DO UPDATE "
                "SET state='approved',digest=excluded.digest,decided_at=excluded.decided_at",
                (approval_id, target_type, target_id, version, row["digest"], actor, self.clock()),
            )
            if target_type == "design":
                connection.execute(
                    "UPDATE design_documents SET state='approved' WHERE id=? AND version=?",
                    (target_id, version),
                )
            else:
                connection.execute(
                    "UPDATE task_plans SET state='approved' WHERE id=?", (target_id,)
                )
                self._materialize_plan_draft(connection, target_id)
            self._event(
                connection,
                target_id,
                "ApprovalRecorded",
                {"target_type": target_type, "version": version},
            )
            return connection.execute(
                "SELECT id FROM approvals WHERE target_type=? AND target_id=? "
                "AND target_version=? AND actor=?",
                (target_type, target_id, version, actor),
            ).fetchone()[0]

    def _materialize_plan_draft(self, connection, plan_id: str) -> None:
        """If plan was created as a handoff draft, materialize tasks on approval."""
        existing = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE plan_id=?", (plan_id,)
        ).fetchone()[0]
        if existing > 0:
            return
        event = connection.execute(
            "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='PlanDraftCreated' "
            "ORDER BY version DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        if event is None:
            return
        payload = json.loads(event[0])
        pending_tasks = payload.get("pending_tasks", [])
        if not pending_tasks:
            return
        plan = connection.execute("SELECT * FROM task_plans WHERE id=?", (plan_id,)).fetchone()
        expected = digest(redact(pending_tasks, self.secrets))
        if plan["digest"] != expected:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Plan draft task digest mismatch.")
        validate_dag(pending_tasks)
        project_id = plan["project_id"]
        for task in pending_tasks:
            connection.execute(
                "INSERT INTO tasks(id,project_id,plan_id,title,priority,state,definition_json) "
                "VALUES(?,?,?,?,?,'approval_pending',?)",
                (
                    task["id"],
                    project_id,
                    plan_id,
                    task["title"],
                    task.get("priority", "medium"),
                    self._json(task),
                ),
            )
        for task in pending_tasks:
            for dep in task.get("depends_on", []):
                connection.execute(
                    "INSERT INTO task_dependencies VALUES(?,?)", (task["id"], dep)
                )
        for task in pending_tasks:
            for link in connection.execute(
                "SELECT rl.* FROM requirement_links rl JOIN handoff_imports hi ON hi.id=rl.import_id "
                "WHERE hi.plan_id=? AND rl.kind='plan_task' AND rl.target_ref=?",
                (plan_id, task["id"]),
            ).fetchall():
                connection.execute(
                    "INSERT INTO requirement_links(id,import_id,requirement_id,kind,target_ref,task_id,module_id,evidence_digest,state) "
                    "VALUES(?,?,?,'task',?,?,?,?,'mapped')",
                    (identifier(),link["import_id"],link["requirement_id"],task["id"],task["id"],link["module_id"],link["evidence_digest"]),
                )
        # Follow-up FK is assigned only after authentic tasks and REQ links exist,
        # within the same explicit plan-approval transaction.
        changes = connection.execute(
            "SELECT c.* FROM requirement_changes c JOIN handoff_imports h ON h.id=c.target_import_id WHERE h.plan_id=? AND c.state='applied'",
            (plan_id,),
        ).fetchall()
        task_ids = {t["id"] for t in pending_tasks}
        for change in changes:
            for impact in json.loads(change["proposal_json"])["impacts"]:
                followup = impact.get("planned_followup_task_id")
                if impact["task_id"] is None or impact["disposition"] == "excluded":
                    continue
                if followup not in task_ids:
                    raise DomainError(ErrorCode.POLICY_VIOLATION, "Invalid frozen follow-up mapping.")
                connection.execute(
                    "UPDATE requirement_change_impacts SET followup_task_id=? WHERE change_id=? AND task_id=? AND requirement_id=?",
                    (followup, change["id"], impact["task_id"], impact["requirement_id"]),
                )
        self._event(
            connection,
            plan_id,
            "PlanTasksMaterialized",
            {"task_ids": [t["id"] for t in pending_tasks]},
        )

    def _design_approved(self, connection, design_id: str, version: int) -> bool:
        latest = connection.execute(
            "SELECT MAX(version) FROM design_documents WHERE id=?", (design_id,)
        ).fetchone()[0]
        return (
            latest == version
            and connection.execute(
                "SELECT 1 FROM approvals a JOIN design_documents d ON d.id=a.target_id "
                "AND d.version=a.target_version AND d.digest=a.digest WHERE a.target_type='design' "
                "AND a.target_id=? AND a.target_version=? AND a.state='approved'",
                (design_id, version),
            ).fetchone()
            is not None
        )

    def create_plan(
        self, project_id: str, plan_id: str, design_id: str, design_version: int, tasks: list[dict]
    ) -> None:
        validate_dag(tasks)
        with self.db.transaction() as connection:
            if not self._design_approved(connection, design_id, design_version):
                raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Current design approval required.")
            owner = connection.execute(
                "SELECT project_id FROM design_documents WHERE id=? AND version=?",
                (design_id, design_version),
            ).fetchone()[0]
            if owner != project_id:
                raise DomainError(ErrorCode.POLICY_VIOLATION, "Design belongs to another project.")
            connection.execute(
                "INSERT INTO task_plans VALUES(?,?,?,?,?,?, 'approval_pending')",
                (
                    plan_id,
                    project_id,
                    design_id,
                    design_version,
                    1,
                    digest(redact(tasks, self.secrets)),
                ),
            )
            for task in tasks:
                connection.execute(
                    "INSERT INTO tasks(id,project_id,plan_id,title,priority,state,definition_json) "
                    "VALUES(?,?,?,?,?,'approval_pending',?)",
                    (
                        task["id"],
                        project_id,
                        plan_id,
                        task["title"],
                        task.get("priority", "medium"),
                        self._json(task),
                    ),
                )
            for task in tasks:
                for dependency in task.get("depends_on", []):
                    connection.execute(
                        "INSERT INTO task_dependencies VALUES(?,?)", (task["id"], dependency)
                    )
            self._event(connection, plan_id, "PlanCreated", {"task_ids": [t["id"] for t in tasks]})

    @staticmethod
    def _record(row) -> TaskRecord:
        return TaskRecord(
            row["id"],
            row["project_id"],
            TaskStatus(row["state"]),
            row["version"],
            json.loads(row["definition_json"]),
            row["snapshot_id"],
            row["plan_id"],
        )

    def task(self, task_id: str) -> TaskRecord:
        rows = self.db.rows("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown task ID.")
        return self._record(rows[0])

    def _approved(self, connection, task_id: str) -> bool:
        plan = connection.execute(
            "SELECT p.* FROM task_plans p JOIN tasks t ON t.plan_id=p.id WHERE t.id=?", (task_id,)
        ).fetchone()
        return (
            plan is not None
            and self._design_approved(connection, plan["design_id"], plan["design_version"])
            and connection.execute(
                "SELECT 1 FROM approvals WHERE target_type='plan' AND target_id=? AND target_version=? "
                "AND digest=? AND state='approved'",
                (plan["id"], plan["version"], plan["digest"]),
            ).fetchone()
            is not None
        )

    def approved(self, task_id: str) -> bool:
        with self.db.transaction() as connection:
            return bool(self._approved(connection, task_id))

    def create_snapshot(self, task_id: str, resolved: dict) -> str:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or not self._approved(connection, task_id):
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED, "Current design and plan approvals required."
                )
            if row["snapshot_id"]:
                return row["snapshot_id"]
            snapshot_id = identifier()
            safe = json.loads(self._json(resolved))
            plan = connection.execute(
                "SELECT * FROM task_plans WHERE id=?", (row["plan_id"],)
            ).fetchone()
            design = connection.execute(
                "SELECT digest FROM design_documents WHERE id=? AND version=?",
                (plan["design_id"], plan["design_version"]),
            ).fetchone()
            approved_content = connection.execute(
                "SELECT content FROM design_documents WHERE id=? AND version=?",
                (plan["design_id"], plan["design_version"]),
            ).fetchone()[0]
            if safe.get("design") != approved_content:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Snapshot design differs from approved document."
                )
            safe["approval_binding"] = {
                "design_digest": design[0],
                "design_version": plan["design_version"],
                "plan_digest": plan["digest"],
                "plan_version": plan["version"],
            }
            connection.execute(
                "INSERT INTO input_snapshots VALUES(?,?,?,?,?)",
                (snapshot_id, task_id, digest(safe), canonical(safe), self.clock()),
            )
            target = transition_task(
                TaskStatus(row["state"]),
                TaskStatus.READY,
                TransitionContext(design_approved=True, plan_approved=True, snapshot_created=True),
            )
            connection.execute(
                "UPDATE tasks SET snapshot_id=?,state=?,version=version+1 WHERE id=?",
                (snapshot_id, target.value, task_id),
            )
            self._event(
                connection,
                task_id,
                "TaskApproved",
                {"snapshot_id": snapshot_id, "digest": digest(safe)},
            )
            return snapshot_id

    def snapshot(self, task_id: str) -> dict:
        rows = self.db.rows(
            "SELECT s.* FROM input_snapshots s JOIN tasks t ON t.snapshot_id=s.id WHERE t.id=?",
            (task_id,),
        )
        if not rows:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Input snapshot required.")
        row = rows[0]
        result = json.loads(row["resolved_json"])
        if digest(result) != row["digest"]:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Input snapshot digest mismatch.")
        return {"id": row["id"], "digest": row["digest"], "resolved": result}

    def dependencies_complete(self, task_id: str) -> bool:
        rows = self.db.rows(
            "SELECT t.state FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_task_id "
            "WHERE d.task_id=?",
            (task_id,),
        )
        return all(row["state"] == "completed" for row in rows)

    def assert_lease(self, connection, lease: dict) -> None:
        row = connection.execute(
            "SELECT * FROM leases WHERE resource=?", (lease["resource"],)
        ).fetchone()
        if (
            row is None
            or row["holder"] != lease["holder"]
            or row["token"] != lease["token"]
            or row["expires_at"] <= self.clock()
        ):
            raise DomainError(ErrorCode.LEASE_CONFLICT, "Lease expired or superseded.")

    def record_usage_thresholds(self, task_id, policy) -> list:
        """Record each newly crossed estimated-budget threshold once (with the usage write path)."""
        from agent_dev.adapters.execution_view import usage_calls
        from agent_dev.domain.usage_policy import crossed_thresholds, estimate, normalize_policy

        policy = normalize_policy(policy)
        if policy is None:
            return []
        summary = estimate(policy, usage_calls(self.db, task_id))
        crossed = crossed_thresholds(policy, summary)
        if not crossed:
            return []
        recorded = []
        with self.db.transaction() as connection:
            existing = {
                json.loads(row["payload_json"]).get("threshold")
                for row in connection.execute(
                    "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='UsageThresholdReached'",
                    (task_id,),
                ).fetchall()
            }
            for threshold in crossed:
                if threshold in existing:
                    continue
                self._event(
                    connection,
                    task_id,
                    "UsageThresholdReached",
                    {
                        "threshold": threshold,
                        "known_cost": summary["known_cost"],
                        "budget": summary["budget"],
                        "currency": summary["currency"],
                        "unknown_calls": summary["unknown_calls"],
                    },
                )
                recorded.append(threshold)
        return recorded

    def assert_change_allowed(self, task_id, connection=None):
        from agent_dev.adapters.requirement_changes import execution_guard, guard_in_transaction

        if connection is not None:
            return guard_in_transaction(connection, task_id, enforce=True)
        return execution_guard(self.db, task_id, enforce=True)

    def transition(
        self,
        task_id: str,
        target: TaskStatus,
        context: TransitionContext,
        lease: dict | None = None,
    ) -> TaskRecord:
        with self.db.transaction() as connection:
            if target in {TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.VERIFYING, TaskStatus.PR_READY, TaskStatus.USER_REVIEW, TaskStatus.COMPLETED}:
                self.assert_change_allowed(task_id, connection)
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise DomainError(ErrorCode.INVALID_INPUT, "Unknown task ID.")
            if context.expected_version is None:
                raise DomainError(ErrorCode.INVALID_INPUT, "Expected version required.")
            if lease is not None:
                self._task_lease(task_id, lease)
                self.assert_lease(connection, lease)
            if target in {TaskStatus.READY, TaskStatus.RUNNING} and not self._approved(
                connection, task_id
            ):
                raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Current approvals invalidated.")
            if target == TaskStatus.RUNNING and lease is None:
                raise DomainError(ErrorCode.LEASE_CONFLICT, "Task lease required.")
            if target == TaskStatus.RUNNING:
                dependencies = connection.execute(
                    "SELECT t.state FROM task_dependencies d JOIN tasks t ON t.id=d.depends_on_task_id "
                    "WHERE d.task_id=?",
                    (task_id,),
                ).fetchall()
                context = replace(
                    context,
                    dependencies_completed=all(r[0] == "completed" for r in dependencies),
                    lease_acquired=True,
                )
                if not row["snapshot_id"]:
                    raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Input snapshot required.")
            if target == TaskStatus.VERIFYING:
                snapshot = connection.execute(
                    "SELECT resolved_json FROM input_snapshots WHERE id=?", (row["snapshot_id"],)
                ).fetchone()
                phases = json.loads(snapshot[0]).get("phases", []) if snapshot else []
                passed = {
                    r[0]
                    for r in connection.execute(
                        "SELECT phase_id FROM phase_attempts WHERE task_id=? AND generation=? AND state='passed'",
                        (task_id, row["generation"]),
                    )
                }
                resolved = json.loads(snapshot[0]) if snapshot else {}
                if resolved.get("workflow_graph") and not resolved.get("workflow_legacy", False):
                    states = {r["node_id"]: r["state"] for r in connection.execute(
                        "SELECT node_id,state FROM workflow_node_runs WHERE task_id=? AND generation=?",
                        (task_id, row["generation"]),
                    )}
                    graph_complete = all(states.get(n["id"]) in {"passed", "skipped"}
                                         for n in resolved["workflow_graph"]["nodes"])
                    context = replace(context, phases_passed=graph_complete)
                else:
                    context = replace(
                        context, phases_passed=bool(phases) and all(p["id"] in passed for p in phases)
                    )
            if target == TaskStatus.PR_READY:
                evidence = connection.execute(
                    "SELECT state FROM verification_runs WHERE task_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                context = replace(
                    context, verification_passed=evidence is not None and evidence[0] == "passed"
                )
            next_state = transition_task(
                TaskStatus(row["state"]), target, replace(context, current_version=row["version"])
            )
            connection.execute(
                "UPDATE tasks SET state=?,version=version+1 WHERE id=?", (next_state.value, task_id)
            )
            self._event(
                connection, task_id, "TaskStateChanged", {"from": row["state"], "to": target.value}
            )
            return self._record(
                connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            )

    @staticmethod
    def _task_lease(task_id: str, lease: dict) -> None:
        if lease.get("resource") != f"task:{task_id}":
            raise DomainError(ErrorCode.LEASE_CONFLICT, "Lease belongs to another task.")

    def generation(self, task_id: str) -> int:
        return self.db.rows("SELECT generation FROM tasks WHERE id=?", (task_id,))[0]["generation"]

    def begin_attempt(self, task_id: str, phase_id: str, key: str, lease: dict) -> dict:
        self._task_lease(task_id, lease)
        with self.db.transaction() as connection:
            self.assert_change_allowed(task_id, connection)
            self.assert_lease(connection, lease)
            task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if (
                task is None
                or task["state"] != "running"
                or not self._approved(connection, task_id)
            ):
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED, "Phase requires an approved running task."
                )
            existing = connection.execute(
                "SELECT * FROM phase_attempts WHERE idempotency_key=? "
                "AND state IN ('running','passed')",
                (key,),
            ).fetchone()
            if existing and (
                existing["task_id"] != task_id
                or existing["phase_id"] != phase_id
                or existing["generation"] != task["generation"]
            ):
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION,
                    "Phase idempotency key belongs to another execution.",
                )
            if existing and existing["state"] == "passed":
                return dict(existing)
            if existing:
                # Only a new fencing token may recover a crashed running attempt.
                if existing["lease_token"] == lease["token"]:
                    raise DomainError(ErrorCode.LEASE_CONFLICT, "Phase already running.")
                connection.execute(
                    "UPDATE phase_attempts SET state='interrupted',ended_at=? WHERE id=?",
                    (self.clock(), existing["id"]),
                )
            attempt_no = connection.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 FROM phase_attempts "
                "WHERE task_id=? AND phase_id=?",
                (task_id, phase_id),
            ).fetchone()[0]
            generation = connection.execute(
                "SELECT generation FROM tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            attempt_id = identifier()
            connection.execute(
                "INSERT INTO phase_attempts VALUES(?,?,?,?,?,'running',?,?,?,?,NULL)",
                (
                    attempt_id,
                    task_id,
                    phase_id,
                    attempt_no,
                    generation,
                    key,
                    lease["resource"],
                    lease["token"],
                    self.clock(),
                ),
            )
            self._event(
                connection,
                task_id,
                "PhaseStarted",
                {"phase_id": phase_id, "attempt_id": attempt_id},
            )
            return dict(
                connection.execute(
                    "SELECT * FROM phase_attempts WHERE id=?", (attempt_id,)
                ).fetchone()
            )

    def finish_attempt(self, attempt_id: str, state: str, lease: dict, evidence=None) -> None:
        if state not in {"passed", "failed", "interrupted", "waiting_for_capacity"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid phase result.")
        with self.db.transaction() as connection:
            self.assert_lease(connection, lease)
            row = connection.execute(
                "SELECT * FROM phase_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if row is None or row["state"] != "running" or row["lease_token"] != lease["token"]:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Phase attempt is not owned and running."
                )
            self._task_lease(row["task_id"], lease)
            connection.execute(
                "UPDATE phase_attempts SET state=?,ended_at=? WHERE id=?",
                (state, self.clock(), attempt_id),
            )
            if evidence is not None:
                self._event(connection, row["task_id"], "WorkflowRoleResult",
                            {"attempt_id": attempt_id, "result": evidence})
            self._event(
                connection,
                row["task_id"],
                "PhaseFinished",
                {"phase_id": row["phase_id"], "attempt_id": attempt_id, "state": state},
            )

    def passed_attempt(self, task_id: str, phase_id: str, generation: int) -> dict | None:
        rows = self.db.rows(
            "SELECT * FROM phase_attempts WHERE task_id=? AND phase_id=? "
            "AND generation=? AND state='passed' ORDER BY attempt_no DESC LIMIT 1",
            (task_id, phase_id, generation),
        )
        return rows[0] if rows else None

    def reenter(
        self, task_id: str, phase_ids: list[str], fingerprint: str, max_reentries: int, lease: dict
    ) -> int:
        self._task_lease(task_id, lease)
        with self.db.transaction() as connection:
            self.assert_lease(connection, lease)
            row = connection.execute(
                "SELECT * FROM findings WHERE task_id=? AND fingerprint=? AND state='open'",
                (task_id, fingerprint),
            ).fetchone()
            retries = row["retries"] if row else 0
            if retries >= max_reentries:
                raise DomainError(ErrorCode.POLICY_VIOLATION, "Finding retry budget exhausted.")
            if row:
                connection.execute("UPDATE findings SET retries=retries+1 WHERE id=?", (row["id"],))
            else:
                connection.execute(
                    "INSERT INTO findings VALUES(?,?,?,'high',?,'open','',1)",
                    (identifier(), task_id, fingerprint, phase_ids[0]),
                )
            generation = connection.execute(
                "SELECT generation+1 FROM tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            # Reuse unaffected passes in the new generation; never mutate terminal attempts.
            passed = connection.execute(
                "SELECT * FROM phase_attempts WHERE task_id=? AND state='passed' AND generation=?",
                (task_id, generation - 1),
            ).fetchall()
            for attempt in passed:
                if attempt["phase_id"] not in phase_ids:
                    number = connection.execute(
                        "SELECT MAX(attempt_no)+1 FROM phase_attempts WHERE task_id=? AND phase_id=?",
                        (task_id, attempt["phase_id"]),
                    ).fetchone()[0]
                    key = digest(
                        [task_id, attempt["phase_id"], generation, "inherited", attempt["id"]]
                    )
                    connection.execute(
                        "INSERT INTO phase_attempts VALUES(?,?,?,?,?,'passed',?,?,?,?,?)",
                        (
                            identifier(),
                            task_id,
                            attempt["phase_id"],
                            number,
                            generation,
                            key,
                            lease["resource"],
                            lease["token"],
                            self.clock(),
                            self.clock(),
                        ),
                    )
            old_nodes = connection.execute(
                "SELECT * FROM workflow_node_runs WHERE task_id=? AND generation=? AND state IN ('passed','skipped')",
                (task_id, generation - 1),
            ).fetchall()
            for node in old_nodes:
                if node["node_id"] not in phase_ids:
                    connection.execute("INSERT INTO workflow_node_runs VALUES(?,?,?,?,?,?,?)",
                        (task_id,node["node_id"],generation,node["state"],node["input_digest"],node["result_json"],0))
            connection.execute("UPDATE tasks SET generation=?,version=version+1 WHERE id=?", (generation, task_id))
            self._event(
                connection,
                task_id,
                "FindingReentry",
                {"fingerprint": fingerprint, "generation": generation, "phases": phase_ids},
            )
            return generation

    def resolve_findings(self, task_id: str, lease: dict) -> None:
        self._task_lease(task_id, lease)
        with self.db.transaction() as connection:
            self.assert_lease(connection, lease)
            connection.execute(
                "UPDATE findings SET state='resolved' WHERE task_id=? AND state='open'", (task_id,)
            )
            self._event(connection, task_id, "FindingsResolved", {})


    def node_runs(self, task_id, generation=None):
        generation = self.generation(task_id) if generation is None else generation
        rows = self.db.rows("SELECT * FROM workflow_node_runs WHERE task_id=? AND generation=? ORDER BY node_id", (task_id,generation))
        return {row["node_id"]:dict(row,result=json.loads(row["result_json"])) for row in rows}

    def record_node(self,task_id,node_id,generation,state,input_digest,result,lease,expected_version=None):
        self._task_lease(task_id,lease)
        with self.db.transaction() as sql:
            self.assert_change_allowed(task_id, sql)
            self.assert_lease(sql,lease)
            task=sql.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
            if task is None or task["generation"]!=generation or task["state"]!='running' or not self._approved(sql,task_id):
                raise DomainError(ErrorCode.POLICY_VIOLATION,"Late Workflow result or invalid task approval.")
            row=sql.execute("SELECT * FROM workflow_node_runs WHERE task_id=? AND node_id=? AND generation=?",(task_id,node_id,generation)).fetchone()
            if row and expected_version is not None and row['version']!=expected_version:
                raise DomainError(ErrorCode.STALE_VERSION,"Workflow node changed.")
            if row and row['state'] in {'passed','skipped'}:
                if row['state']==state and row['input_digest']==input_digest:return dict(row)
                raise DomainError(ErrorCode.POLICY_VIOLATION,"Terminal Workflow node is immutable.")
            previous=sql.execute("SELECT MAX(version) FROM workflow_node_runs WHERE task_id=? AND node_id=?",(task_id,node_id)).fetchone()[0]
            version=(previous+1) if previous is not None else 0
            sql.execute("INSERT INTO workflow_node_runs VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_id,node_id,generation) DO UPDATE SET state=excluded.state,input_digest=excluded.input_digest,result_json=excluded.result_json,version=excluded.version",
                (task_id,node_id,generation,state,input_digest,self._json(result),version))
            self._event(sql,task_id,'WorkflowNodeChanged',{'node_id':node_id,'generation':generation,'state':state,'version':version,'input_digest':input_digest})
            return dict(sql.execute("SELECT * FROM workflow_node_runs WHERE task_id=? AND node_id=? AND generation=?",(task_id,node_id,generation)).fetchone())

    def approve_node(self,task_id,node_id,actor,decision,expected_version,lease,note=""):
        if not isinstance(actor,str) or not actor.strip() or len(actor)>120 or decision not in {'approved','rejected'} or type(expected_version) is not int:
            raise DomainError(ErrorCode.INVALID_INPUT,"Actor, decision and expected node version required.")
        if not isinstance(note,str) or len(note)>4000:
            raise DomainError(ErrorCode.INVALID_INPUT,"Approval note must be text up to 4000 characters.")
        note=redact(note.strip(),self.secrets)
        self._task_lease(task_id,lease)
        with self.db.transaction() as sql:
            self.assert_change_allowed(task_id, sql)
            self.assert_lease(sql,lease)
            task=sql.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
            if task is None or task['state']!='blocked' or not self._approved(sql,task_id):
                raise DomainError(ErrorCode.APPROVAL_REQUIRED,"Current blocked approved task required.")
            snap=sql.execute("SELECT * FROM input_snapshots WHERE id=?",(task['snapshot_id'],)).fetchone()
            graph=json.loads(snap['resolved_json']).get('workflow_graph',{})
            node=next((n for n in graph.get('nodes',[]) if n['id']==node_id),None)
            if not node or node['kind']!='approval' or node.get('actor',actor)!=actor:
                raise DomainError(ErrorCode.POLICY_VIOLATION,"Approval actor or node identity mismatch.")
            run=sql.execute("SELECT * FROM workflow_node_runs WHERE task_id=? AND node_id=? AND generation=?",(task_id,node_id,task['generation'])).fetchone()
            if run is None or run['state']!='awaiting_approval' or run['version']!=expected_version:
                raise DomainError(ErrorCode.STALE_VERSION,"Approval node version or generation changed.")
            approval_id=identifier()
            sql.execute("INSERT INTO workflow_node_approvals VALUES(?,?,?,?,?,?,?,?,?)",(approval_id,task_id,node_id,task['generation'],snap['digest'],run['input_digest'],actor,decision,self.clock()))
            # The note carries the user's decisions on open questions to later roles.
            sql.execute("UPDATE workflow_node_runs SET state=?,version=version+1,result_json=? WHERE task_id=? AND node_id=? AND generation=?",('passed' if decision=='approved' else 'rejected',self._json({'approval_id':approval_id,'actor':actor,'decision':decision,'note':note}),task_id,node_id,task['generation']))
            self._event(sql,task_id,'WorkflowNodeApproved',{'node_id':node_id,'generation':task['generation'],'decision':decision,'actor':actor,'approval_id':approval_id,'note':note})
            return {'approval_id':approval_id,'decision':decision,'generation':task['generation'],'node_id':node_id}
