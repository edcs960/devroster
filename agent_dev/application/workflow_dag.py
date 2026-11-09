"""Serial DAG execution using existing immutable attempts, budgets and fencing."""

import json
import time
from dataclasses import replace as with_fields

from agent_dev.domain.content import digest
from agent_dev.domain.contracts import PhaseRequest, PhaseResult
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.execution_limits import limits, safe_usage
from agent_dev.domain.role_policy import session_policy
from agent_dev.domain.session_output import instructions, split
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext
from agent_dev.domain.workflow_graph import (
    condition,
    descendants,
    preview,
    schedule,
    session_spans,
)

EFFORTS = ("auto", "low", "medium", "high", "xhigh", "max", "ultra")


def merge_usage(*usages):
    """Add token and time counters of several invocations paid by one node."""
    total = {}
    for usage in usages:
        for key, value in (usage or {}).items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                total[key] = total.get(key, 0) + value
    return total



# Parser messages for a result block that is missing or not valid JSON; anything else
# (unknown fields, bad status, wrong owner) is a real answer and is never "repaired".
FORMAT_ERRORS = frozenset({"Malformed Harness Control JSON.", "Exactly one Harness Control block required."})
REPAIR_NOTE = (
    "Your previous reply in this conversation did not end with exactly one valid Harness Control "
    "JSON block. Do not change, create or delete any file and do not redo the work. Reply with only "
    "that block, in a ```json fence, describing the work you already did."
)


def parse_with_repair(parser, phases, request, result, runners, capture=None, seconds_left=None, record=None):
    """(result, decision); a format error gets one repair request in the same conversation.

    Only a runner that returned a resumable session is asked (a fresh call would redo the
    work and cost a full attempt). The repair must not change files, its time comes out of
    the attempt's remaining budget, and its usage is added to the attempt.
    """
    try:
        return result, parser(result.output, phases)
    except DomainError as exc:
        left = request.timeout_seconds if seconds_left is None else int(seconds_left)
        if str(exc) not in FORMAT_ERRORS or not getattr(result, "resume_session", "") or left < 1:
            raise
    context = json.loads(request.context)
    context["control_repair"] = REPAIR_NOTE
    repair = with_fields(
        request,
        context=json.dumps(context, ensure_ascii=False),
        resume_session=result.resume_session,
        timeout_seconds=max(1, min(request.timeout_seconds, left, 600)),
    )
    before = capture() if capture else None
    repaired = runners.execute(repair)
    if capture and capture() != before:
        raise DomainError(ErrorCode.POLICY_VIOLATION, "Control repair changed files.")
    decision = parser(repaired.output, phases)
    if record:
        record()
    merged = with_fields(
        result,
        usage=merge_usage(result.usage, repaired.usage),
        resume_session=repaired.resume_session or result.resume_session,
    )
    return merged, decision


def verification_reentry(graph):
    """The code role a failed workflow gate returns to: developer, else a developer-* node."""
    roles = [n["id"] for n in graph["nodes"] if n.get("kind") == "role"]
    if "developer" in roles:
        return "developer"
    return next((r for r in roles if r.startswith("developer-")), "")

class DagExecution:
    def __init__(self, coordinator):
        self.c = coordinator
        self.store = coordinator.store
        # Node id -> its section of the current session invocation (ADR 0002).
        self.session_results = {}
        # Session nodes whose section was missing; each runs once on its own.
        self.session_fallback = set()
        # Per-node evidence from the session invocation: one cross-check and the
        # declared vs. observed file changes.
        self.session_cross = {}
        self.session_changes = {}
        # Usage of a session invocation whose paying node had to run again alone.
        self.session_usage = {}

    def checkpoint(self, task_id, lease, reason):
        method = getattr(self.c.artifacts, "checkpoint", None)
        if method:
            method(task_id, lease, reason)

    def block(self, task_id, lease, reason, code=ErrorCode.POLICY_VIOLATION):
        current = self.store.task(task_id)
        target = (
            TaskStatus.WAITING_FOR_CAPACITY
            if code == ErrorCode.CAPACITY_EXHAUSTED
            else TaskStatus.BLOCKED
        )
        self.store.event(task_id, "ExecutionBlocked", {"code": code.value, "reason": reason})
        if current.state == TaskStatus.RUNNING:
            self.store.transition(
                task_id,
                target,
                TransitionContext(
                    capacity_exhausted=target == TaskStatus.WAITING_FOR_CAPACITY,
                    expected_version=current.version,
                ),
                lease,
            )
        self.checkpoint(task_id, lease, reason)
        return {"task_id": task_id, "state": target.value, "error": code.value, "reason": reason}

    def execute(self, task_id, lease, max_reentries=None):
        self.store.assert_change_allowed(task_id)
        task = self.store.task(task_id)
        snapshot = self.store.snapshot(task_id)
        resolved = snapshot["resolved"]
        graph = resolved["workflow_graph"]
        metadata = resolved.get("workflow_metadata", task.definition)
        budget = limits(resolved.get("execution_limits"))
        maximum = (
            budget["max_reentries"]
            if max_reentries is None
            else min(max_reentries, budget["max_reentries"])
        )
        if task.state in {
            TaskStatus.COMPLETED,
            TaskStatus.PR_READY,
            TaskStatus.USER_REVIEW,
            TaskStatus.VERIFYING,
        }:
            return {"task_id": task_id, "state": task.state.value}
        if not self.store.approved(task_id):
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Current task approvals required.")
        missing = preview(graph, metadata)["missing_roles"]
        if missing:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Workflow required roles missing: " + ", ".join(missing)
            )
        states = self.store.node_runs(task_id)
        if any(x["state"] in {"awaiting_approval", "rejected"} for x in states.values()):
            return {
                "task_id": task_id,
                "state": "blocked",
                "reason": "AWAITING_WORKFLOW_APPROVAL"
                if any(x["state"] == "awaiting_approval" for x in states.values())
                else "WORKFLOW_APPROVAL_REJECTED",
            }
        context = TransitionContext(
            design_approved=True,
            plan_approved=True,
            snapshot_created=True,
            dependencies_completed=self.store.dependencies_complete(task_id),
            preflight_passed=self.c.catalog.preflight(task),
            lease_acquired=True,
            expected_version=task.version,
        )
        budget_pause = getattr(self.store, "budget_pause", None)
        if task.state == TaskStatus.BLOCKED and budget_pause:
            due, summary = budget_pause(task_id, self.store.snapshot(task_id)["resolved"].get("usage_policy"))
            if due:
                approve = getattr(self.store, "approve_budget_pause", None)
                if approve:
                    approve(task_id, summary)  # Resuming is the approval.
        if task.state in {TaskStatus.BLOCKED, TaskStatus.WAITING_FOR_CAPACITY}:
            task = self.store.transition(task_id, TaskStatus.READY, context, lease)
        if task.state == TaskStatus.READY:
            from dataclasses import replace

            task = self.store.transition(
                task_id, TaskStatus.RUNNING, replace(context, expected_version=task.version), lease
            )
        elif task.state != TaskStatus.RUNNING:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Task is not executable.")
        self.store.recover_attempts(task_id, lease)
        while True:
            generation = self.store.generation(task_id)
            states = self.store.node_runs(task_id)
            item = schedule(graph, states, metadata)
            if item is None:
                break
            node, enabled = item
            node_id = node["id"]
            # Inputs are transitive output digests, immutable snapshot and topology, not model claims.
            ancestors = {e["from"] for e in graph["edges"] if e["to"] == node_id}
            inputs = digest(
                {
                    "snapshot": snapshot["digest"],
                    "node": node,
                    "parents": {p: states[p]["result_json"] for p in sorted(ancestors)},
                }
            )
            record = lambda state, result, node_id=node_id, generation=generation, inputs=inputs: (
                self.store.record_node(task_id, node_id, generation, state, inputs, result, lease)
            )
            if not enabled:
                # Routing nodes write no checkpoint of their own; the next role's records
                # them (11 checkpoint copies in one second).
                record("skipped", {"reason": "INACTIVE_CONDITION_ROUTE"})
                continue
            kind = node["kind"]
            if kind == "approval":
                record(
                    "awaiting_approval",
                    {"reason": "AWAITING_WORKFLOW_APPROVAL", "actor": node.get("actor", "")},
                )
                return self.block(
                    task_id, lease, "AWAITING_WORKFLOW_APPROVAL", ErrorCode.APPROVAL_REQUIRED
                )
            if kind in {"condition", "merge"}:
                record(
                    "passed",
                    {"condition": condition(node, metadata)}
                    if kind == "condition"
                    else {"reason": "ACTIVE_INPUTS_COMPLETE"},
                )
                continue
            try:
                record("running", {"started_at": self.store.clock()})
                if kind == "verification":
                    results = self.c.catalog.verify_node(task_id, node.get("gate_ids", []), lease)
                    if not results["passed"]:
                        record("failed", results)
                        target = verification_reentry(graph)
                        if not target:
                            return self.block(
                                task_id,
                                lease,
                                "WORKFLOW_VERIFICATION_FAILED",
                                ErrorCode.VERIFICATION_FAILED,
                            )
                        # A failing required gate goes back to the developer with its output,
                        # like a QA issue, within the same retry budget (the task only
                        # stopped, and nothing could send it back).
                        failed = [g for g in results.get("gates", []) if not g.get("passed")]
                        decision = {
                            "handoff_status": "issue",
                            "issue_type": "verification",
                            "owning_agent_id": target,
                            "reentry_step_id": target,
                            "root_cause": "필수 검증 실패: "
                            + ", ".join(f"{g.get('gate')}(exit {g.get('exit_code')})" for g in failed),
                            "gate_output_tail": {
                                str(g.get("gate")): str(g.get("evidence", ""))[-4000:] for g in failed
                            },
                        }
                        fingerprint = digest([task_id, node_id, target, "verification"])
                        self.store.event(
                            task_id,
                            "FindingDetected",
                            {"fingerprint": fingerprint, "decision": decision},
                        )
                        try:
                            self.store.reenter(
                                task_id,
                                [target] + sorted(descendants(graph, target) - {target}),
                                fingerprint,
                                maximum,
                                lease,
                            )
                        except DomainError:  # retry budget spent: still a verification failure
                            return self.block(
                                task_id,
                                lease,
                                "WORKFLOW_VERIFICATION_FAILED",
                                ErrorCode.VERIFICATION_FAILED,
                            )
                        self.session_results.clear()
                        self.session_fallback.clear()
                        self.session_cross.clear()
                        self.session_changes.clear()
                        self.checkpoint(task_id, lease, "reentry: " + target)
                        continue
                    record("passed", results)
                elif kind == "tool":
                    result = self.tool(node, resolved, task_id)
                    # An optional tool that could not run is recorded as passed with its skip
                    # reason: a "skipped" node state would disable the role that follows it.
                    record("passed", result)
                else:
                    result = self.role(task, snapshot, node, generation, budget, lease)
                    decision = result["decision"]
                    if decision["handoff_status"] == "pass":
                        record("passed", result)
                    else:
                        record("failed", result)
                        target = decision.get("reentry_step_id", "")
                        allowed = node.get("reentry_targets", [])
                        if target not in allowed:
                            return self.block(task_id, lease, "OPERATOR_ACTION_REQUIRED")
                        affected = descendants(graph, target)
                        # The retry budget is keyed by what repeats, not by the model's wording:
                        # a rephrased root cause must not reset it. The full decision is
                        # still recorded in the FindingDetected event.
                        fingerprint = digest(
                            [
                                task_id,
                                node_id,
                                target,
                                str(decision.get("issue_type") or "").strip().lower(),
                            ]
                        )
                        self.store.event(
                            task_id,
                            "FindingDetected",
                            {"fingerprint": fingerprint, "decision": decision},
                        )
                        self.store.reenter(
                            task_id,
                            [target] + sorted(affected - {target}),
                            fingerprint,
                            maximum,
                            lease,
                        )
                        # Later session sections were written before this issue; rerun them.
                        self.session_results.clear()
                        self.session_fallback.clear()
                        self.session_cross.clear()
                        self.session_changes.clear()
                        self.checkpoint(task_id, lease, "reentry: " + target)
                        continue
            except DomainError as exc:
                record("failed", {"code": exc.code.value, "reason": str(exc)})
                return self.block(task_id, lease, str(exc), exc.code)
            except Exception:  # noqa: BLE001 - preserve interruption evidence and block safely
                record("interrupted", {"reason": "EXECUTION_INTERRUPTED"})
                return self.block(
                    task_id, lease, "EXECUTION_INTERRUPTED", ErrorCode.EXTERNAL_TRANSIENT
                )
            self.checkpoint(task_id, lease, "node complete: " + node_id)
        current = self.store.task(task_id)
        self.store.transition(
            task_id,
            TaskStatus.VERIFYING,
            TransitionContext(phases_passed=True, expected_version=current.version),
            lease,
        )
        self.checkpoint(task_id, lease, "active graph complete; project verification pending")
        return {"task_id": task_id, "state": "verifying", "snapshot_digest": snapshot["digest"]}

    def tool(self, node, resolved, task_id=None):
        if self.c.tools is None:
            if not node.get("required", True):
                return {"state": "skipped", "reason": "TOOL_HOST_UNAVAILABLE"}
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE, "Required Workflow tool host unavailable."
            )
        grants = sorted(
            resolved.get("tools", []),
            key=lambda grant: bool(grant.get("binding_digest")),
            reverse=True,
        )
        permitted = next(
            (
                g
                for g in grants
                if g.get("connection_id", g.get("id")) == node["connection_id"]
                and node["operation"] in g.get("operations", [])
                and node.get("scope", "") == g.get("scope", "")
                and node["capability"] in g.get("capabilities", [])
            ),
            None,
        )
        if permitted is None:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "Workflow tool exceeds snapshot operation/capability/scope grant.",
            )
        tool_budget = node.get("budget", {})
        if task_id is not None:
            calls = self.store.db.rows(
                "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='WorkflowToolCall'",
                (task_id,),
            )
            count = sum(json.loads(row["payload_json"]).get("node_id") == node["id"] for row in calls)
            if count >= tool_budget.get("max_calls", 10):
                raise DomainError(ErrorCode.POLICY_VIOLATION, "TOOL_CALL_LIMIT_REACHED")
            self.store.event(task_id, "WorkflowToolCall", {"node_id": node["id"], "call": count + 1})
        try:
            kwargs = {"expected_grant": permitted} if permitted.get("binding_digest") else {}
            if permitted.get("binding_digest") or "timeout_seconds" in tool_budget:
                kwargs["timeout_seconds"] = min(30, tool_budget.get("timeout_seconds", 30))
            result = self.c.tools(
                node["connection_id"],
                node["operation"],
                node.get("arguments", {}),
                scope=node.get("scope", ""),
                **kwargs,
            )
        except DomainError as exc:
            if node.get("required", True):
                raise
            return {"state": "skipped", "reason": exc.code.value}
        if not isinstance(result, dict) or len(json.dumps(result).encode()) > 1_000_000:
            raise DomainError(ErrorCode.INVALID_INPUT, "Tool bundle exceeds evidence limit.")
        return dict(result, trust="untrusted_data")

    def role(self, task, snapshot, node, generation, budget, lease):
        task_id = task.task_id
        node_id = node["id"]
        passed = self.store.passed_attempt(task_id, node_id, generation)
        if passed:
            evidence = self.store.db.rows(
                "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='WorkflowRoleResult' ORDER BY version DESC",
                (task_id,),
            )
            for item in evidence:
                payload = json.loads(item["payload_json"])
                if payload.get("attempt_id") == passed["id"]:
                    return payload["result"]
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Passed attempt missing Workflow node evidence.")
        usage = self.store.execution_usage(task_id)
        remaining = budget["max_runner_seconds"] - usage["runner_seconds"]
        if usage["attempts"] >= budget["max_attempts"] or remaining < 1:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "EXECUTION_LIMIT_REACHED")
        budget_pause = getattr(self.store, "budget_pause", None)
        if budget_pause and budget_pause(task_id, snapshot["resolved"].get("usage_policy"))[0]:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "BUDGET_PAUSE")
        recover = getattr(self.c.catalog, "recover_interrupted", None)
        if recover:
            recover(task_id, node_id)  # A crashed attempt's leftovers are checked before rerunning.
        attempt = self.store.begin_attempt(
            task_id, node_id, digest([task_id, node_id, snapshot["digest"], generation]), lease
        )
        if attempt["state"] == "passed":
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Passed attempt missing Workflow node evidence."
            )
        resolved = snapshot["resolved"]
        context = json.loads(self.c.artifacts.context(task_id, node_id))
        context["role_policy"] = resolved.get("role_policies", {}).get(node_id, {})
        context["node_config"] = node
        context["tool_evidence_untrusted"] = [
            s["result"]
            for s in self.store.node_runs(task_id).values()
            if s["state"] == "passed" and s["result"].get("trust") == "untrusted_data"
        ]
        timeout = min(
            node.get("timeout_seconds", budget["phase_timeout_seconds"]),
            budget["phase_timeout_seconds"],
            int(remaining),
        )
        request = PhaseRequest(
            task,
            node_id,
            attempt["id"],
            snapshot["digest"],
            generation,
            frozenset(context["role_policy"].get("capabilities", ["code.read"])),
            json.dumps(context, ensure_ascii=False),
            timeout_seconds=max(1, timeout),
        )
        batch = None
        fallback = node_id in self.session_fallback
        if node.get("session") and node_id not in self.session_results and not fallback:
            try:
                batch = self.session_request(
                    task, snapshot, node, generation, attempt, budget, remaining
                )
            except BaseException:
                self.store.finish_attempt(attempt["id"], "failed", lease)
                raise
        self.c.artifacts.prompt(
            task_id, node_id, attempt["id"], (batch or request).context
        )
        result = None
        # A session member takes its section of the session's single AI call.
        ai_call = bool(batch)
        started = time.monotonic()
        try:
            capture = getattr(self.c.catalog, "role_before", None)
            if batch:
                self.run_session(task_id, batch, capture, resolved["workflow_graph"])
            if node.get("session") and node_id in self.session_results:
                # Session changes were checked once against the union of member policies.
                before = None
                result = self.session_results.pop(node_id)
            elif node.get("session") and node_id not in self.session_fallback:
                raise DomainError(ErrorCode.POLICY_VIOLATION, "Session node result missing.")
            else:
                # A plain node, or a session node whose section was missing: run it alone once.
                fallback = node_id in self.session_fallback
                self.session_fallback.discard(node_id)
                before = capture(task_id, node_id) if capture else None
                save_baseline = getattr(self.c.catalog, "save_baseline", None)
                if save_baseline and before is not None:
                    save_baseline(task_id, node_id, attempt["id"], before)
                ai_call = True
                result = self.c.runners.execute(request)
                if node_id in self.session_usage:
                    # The failed session invocation was paid too; keep it in this node's usage.
                    result = with_fields(
                        result, usage=merge_usage(self.session_usage.pop(node_id), result.usage)
                    )
            # Preserve malformed output as fenced evidence; it cannot apply structured edits.
            self.c.artifacts.write(task_id, node_id, attempt["id"], result.output)
            node_ids = {n["id"] for n in resolved["workflow_graph"]["nodes"]}
            if node.get("session") and before is None:
                # A session member's result came from the shared call: nothing to resume alone.
                decision = self.c.parser(result.output, node_ids)
            else:
                result, decision = parse_with_repair(
                    self.c.parser,
                    node_ids,
                    request,
                    result,
                    self.c.runners,
                    capture=(lambda: capture(task_id, node_id)) if capture else None,
                    seconds_left=remaining - (time.monotonic() - started),
                    record=lambda: self.store.event(
                        task_id, "ControlRepairRequested", {"attempt_id": attempt["id"], "node_id": node_id}
                    ),
                )
            cross_policy = node.get("cross_check", "off")
            review_remaining = int(remaining - (time.monotonic() - started))
            if node_id in self.session_cross:
                # Already reviewed once with the whole session output.
                cross = self.session_cross.pop(node_id)
            elif cross_policy != "off" and review_remaining < 1:
                if cross_policy == "required":
                    raise DomainError(ErrorCode.POLICY_VIOLATION, "EXECUTION_LIMIT_REACHED")
                cross = {"state": "skipped", "reason": "EXECUTION_LIMIT_REACHED"}
            else:
                from dataclasses import replace

                cross = self.cross_check(
                    replace(
                        request,
                        timeout_seconds=max(1, min(request.timeout_seconds, review_remaining)),
                    ),
                    result,
                    cross_policy,
                )
            method = getattr(self.c.catalog, "apply_role_result", None)
            if method:
                if decision["handoff_status"] == "pass":
                    method(task_id, node_id, result.output, before, lease, generation)
                else:
                    method(task_id, node_id, "", before, lease, generation)
            hook_results = []
            hooks = getattr(self.c, "hooks", None)
            if hooks and decision["handoff_status"] == "pass" and "code.write" in request.capabilities:
                # after_edit hooks (lint, format checks): a failure sends the node back to fix it.
                hook_results = hooks(task_id, "after_edit", {"phase": node_id})
                failed = next((r for r in hook_results if not r["ok"]), None)
                if failed:
                    decision = {
                        "handoff_status": "issue",
                        "issue_type": "implementation",
                        "root_cause": f"after_edit 훅 '{failed['name']}' 실패 (종료 코드 {failed['exit_code']}): "
                        + failed["output"][-1500:],
                        "owning_agent_id": node_id,
                        "reentry_step_id": node_id,
                    }
            evidence = {
                "decision": decision,
                "attempt_id": attempt["id"],
                "output_digest": digest(result.output),
                "provider": result.provider,
                "selection": result.selection,
                "cross_check": cross,
            }
            if getattr(result, "resume_session", ""):
                evidence["resume_session"] = result.resume_session
            if fallback:
                evidence["session_fallback"] = True
            if node_id in self.session_changes:
                evidence["session_changes"] = self.session_changes.pop(node_id)
            if hook_results:
                evidence["hooks"] = [{k: r[k] for k in ("name", "ok", "exit_code")} for r in hook_results]
            self.store.finish_attempt(
                attempt["id"], "passed" if decision["handoff_status"] == "pass" else "failed", lease,
                evidence=evidence,
            )
            return evidence
        except BaseException as exc:
            state = (
                "waiting_for_capacity"
                if isinstance(exc, DomainError) and exc.code == ErrorCode.CAPACITY_EXHAUSTED
                else "failed"
                if isinstance(exc, Exception)
                else "interrupted"
            )
            self.store.finish_attempt(attempt["id"], state, lease)
            raise
        finally:
            self.store.event(
                task_id,
                "RunnerUsage",
                {
                    "attempt_id": attempt["id"],
                    "phase_id": node_id,
                    "runner_seconds": round(time.monotonic() - started, 6),
                    "provider": result.provider if result else "",
                    "usage": safe_usage(result.usage) if result else {},
                    "selection": result.selection if result else {},
                    "ai_call": ai_call,
                },
            )

    def session_members(self, task_id, graph, metadata, node):
        """Role nodes the serial scheduler would run next inside ``node``'s session."""
        span = set(session_spans(graph).get(node["session"], []))
        states = dict(self.store.node_runs(task_id))
        members = []
        while True:
            item = schedule(graph, states, metadata)
            if item is None or item[0]["id"] not in span:
                break
            current, enabled = item
            if current["kind"] == "role" and enabled:
                members.append(current)
            states[current["id"]] = {"state": "passed" if enabled else "skipped"}
        return members

    def session_request(self, task, snapshot, node, generation, attempt, budget, remaining):
        resolved = snapshot["resolved"]
        graph = resolved["workflow_graph"]
        members = self.session_members(
            task.task_id, graph, resolved.get("workflow_metadata", task.definition), node
        )
        if not members or members[0]["id"] != node["id"]:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Session schedule mismatch.")
        ids = [m["id"] for m in members]
        policies = resolved.get("role_policies", {})
        policy = session_policy([policies.get(i, {}) for i in ids])
        context = json.loads(self.c.artifacts.session_context(task.task_id, node["session"], ids))
        # The context stays complete; the runner that really resumes trims it.
        resume = self.resume_session(task.task_id, graph, node, generation)
        context["role_policy"] = policy
        context["node_config"] = session_config(node["session"], members)
        context["tool_evidence_untrusted"] = [
            s["result"]
            for s in self.store.node_runs(task.task_id).values()
            if s["state"] == "passed" and s["result"].get("trust") == "untrusted_data"
        ]
        phase = budget["phase_timeout_seconds"]
        total = sum(min(m.get("timeout_seconds", phase), phase) for m in members)
        if budget.get("session_timeout_seconds"):
            total = min(total, budget["session_timeout_seconds"])
        request = PhaseRequest(
            task,
            node["id"],
            attempt["id"],
            snapshot["digest"],
            generation,
            frozenset(policy["capabilities"]),
            json.dumps(context, ensure_ascii=False),
            timeout_seconds=max(1, min(total, int(remaining))),
            node_ids=tuple(ids),
            resume_session=resume,
        )
        return request

    def resume_session(self, task_id, graph, node, generation):
        """Earlier conversation of the session that owns the latest reentry target, if any.

        Only the session being fixed continues its conversation; downstream sessions that
        rerun as descendants start fresh so verification stays independent.
        """
        if generation < 1:
            return ""
        span = session_spans(graph).get(node["session"], [])
        findings = self.store.db.rows(
            "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='FindingDetected' ORDER BY version DESC LIMIT 1",
            (task_id,),
        )
        target = (
            json.loads(findings[0]["payload_json"]).get("decision", {}).get("reentry_step_id")
            if findings
            else None
        )
        if target not in span:
            return ""
        attempts = {
            row["id"]
            for row in self.store.db.rows(
                "SELECT id,phase_id FROM phase_attempts WHERE task_id=?", (task_id,)
            )
            if row["phase_id"] in span
        }
        for row in self.store.db.rows(
            "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='WorkflowRoleResult' ORDER BY version DESC",
            (task_id,),
        ):
            payload = json.loads(row["payload_json"])
            if payload.get("attempt_id") in attempts and payload["result"].get("resume_session"):
                return payload["result"]["resume_session"]
        return ""

    def run_session(self, task_id, request, capture, graph):
        before = capture(task_id, request.phase_id) if capture else None
        result = self.c.runners.execute(request)
        keep = getattr(self.c.artifacts, "session_output", None)
        if keep:
            keep(task_id, request.phase_id, request.attempt_id, result.output)
        sections = split(result.output, request.node_ids)
        if self.sections_missing(sections, request.node_ids, graph) and getattr(
            result, "resume_session", ""
        ):
            # One read-only format repair in the same conversation before falling back to
            # one invocation per node.
            repaired = self.c.runners.execute(
                with_fields(
                    request,
                    capabilities=frozenset({"code.read"}),
                    context=json.dumps(
                        {
                            "format_repair": (
                                "직전 답변의 내용은 그대로 두고, 아래 형식으로만 다시 출력하라. "
                                "파일을 읽거나 바꾸지 말고 작업을 다시 하지 마라."
                            ),
                            "output_contract": instructions(request.node_ids),
                        },
                        ensure_ascii=False,
                    ),
                    resume_session=result.resume_session,
                    timeout_seconds=min(request.timeout_seconds, 600),
                )
            )
            if keep:
                keep(task_id, request.phase_id, request.attempt_id, repaired.output, repair=True)
            fixed = split(repaired.output, request.node_ids)
            if all(fixed[n] is not None for n in request.node_ids):
                sections = fixed
            result = with_fields(result, usage=merge_usage(result.usage, repaired.usage))
        declared = self.declared_changes(sections, graph)
        enforce = getattr(self.c.catalog, "enforce_session", None)
        if enforce and before is not None:
            changes = enforce(task_id, list(request.node_ids), before, declared)
            if changes and changes.get("changed"):
                for node_id, paths in declared.items():
                    self.session_changes[node_id] = {"changed_paths": paths}
                self.session_changes.setdefault(request.phase_id, {})["unattributed_paths"] = (
                    changes.get("unattributed", [])
                )
        self.session_cross_check(request, result, sections, graph)
        for node_id in request.node_ids:
            section = sections[node_id]
            if section is None:
                self.session_fallback.add(node_id)
                if node_id == request.phase_id:
                    self.session_usage[node_id] = dict(result.usage)
                continue
            paid = node_id == request.phase_id
            self.session_results[node_id] = PhaseResult(
                section,
                result.provider,
                # Usage and the resumable conversation belong to the node that paid for it.
                result.usage if paid else {},
                result.selection,
                resume_session=getattr(result, "resume_session", "") if paid else "",
            )

    def sections_missing(self, sections, node_ids, graph):
        """Whether the answer lacks sections it should have had. Sections after a node that
        reported an issue are absent on purpose (the contract says to stop there)."""
        allowed = {n["id"] for n in graph["nodes"]}
        for node_id in node_ids:
            if sections[node_id] is None:
                return True
            try:
                if self.c.parser(sections[node_id], allowed).get("handoff_status") != "pass":
                    return False
            except DomainError:
                return False  # The node's own parse reports this; reformatting cannot help.
        return False

    def declared_changes(self, sections, graph):
        """Node id -> paths it says it changed (`changed_paths` in its control JSON)."""
        allowed = {n["id"] for n in graph["nodes"]}
        declared = {}
        for node_id, section in sections.items():
            if section is None:
                continue
            try:
                decision = self.c.parser(section, allowed)
            except DomainError:
                continue  # The node's own parse reports this later.
            paths = [p.strip() for p in decision.get("changed_paths", "").split(",") if p.strip()]
            if paths:
                declared[node_id] = paths
        return declared

    def session_cross_check(self, request, result, sections, graph):
        """One independent review of the whole session with its strictest member policy."""
        order = {"off": 0, "optional": 1, "required": 2}
        nodes = {n["id"]: n for n in graph["nodes"]}
        policy = max(
            (nodes[i].get("cross_check", "off") for i in request.node_ids),
            key=order.__getitem__,
        )
        if policy == "off":
            return
        cross = dict(self.cross_check(request, result, policy), scope="session")
        for node_id, section in sections.items():
            if section is not None:
                self.session_cross[node_id] = cross

    def cross_check(self, request, result, policy):
        if policy == "off":
            return {"state": "disabled"}
        method = getattr(self.c.runners, "cross_check", None)
        if method is None:
            if policy == "required":
                raise DomainError(
                    ErrorCode.CAPABILITY_UNAVAILABLE,
                    "Required independent cross-check unavailable.",
                )
            return {"state": "skipped", "reason": "INDEPENDENT_REVIEW_UNAVAILABLE"}
        return method(request, result, required=policy == "required")


def session_config(session, members):
    """Runner settings for a session: shared module/provider/model, the highest effort."""
    config = {"id": session, "kind": "role", "role_id": members[0]["role_id"], "session": session}
    for field in ("module", "provider", "model"):
        value = next((m[field] for m in members if m.get(field)), None)
        if value:
            config[field] = value
    efforts = [m["effort"] for m in members if m.get("effort", "auto") != "auto"]
    if efforts:
        config["effort"] = max(efforts, key=EFFORTS.index)
    return config
