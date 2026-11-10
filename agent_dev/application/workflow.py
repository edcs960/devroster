"""Small coordinator: policy and scheduling, never provider or filesystem work."""

import json
import time
from dataclasses import replace

from agent_dev.domain.content import digest
from agent_dev.domain.context_policy import profile as context_profile
from agent_dev.domain.contracts import PhaseRequest
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.execution_limits import limits, safe_usage
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext
from agent_dev.ports.workflow import ArtifactPort, PhaseCatalogPort, RunnerPort, WorkflowStore


class WorkflowCoordinator:
    def __init__(
        self,
        store: WorkflowStore,
        runners: RunnerPort,
        artifacts: ArtifactPort,
        catalog: PhaseCatalogPort,
        parser,
        *,
        tools=None,
        hooks=None,
    ):
        self.store, self.runners, self.artifacts = store, runners, artifacts
        self.catalog, self.parser, self.tools = catalog, parser, tools
        # Project hooks (task_id, event, context) -> results; run by DevRoster, not the AI.
        self.hooks = hooks

    def execute(self, task_id: str, lease: dict, max_reentries: int | None = None) -> dict:
        self.store.assert_change_allowed(task_id)
        initial = self.store.snapshot(task_id)["resolved"]
        if initial.get("workflow_graph") and not initial.get("workflow_legacy", False):
            from .workflow_dag import DagExecution

            return DagExecution(self).execute(task_id, lease, max_reentries)
        task = self.store.task(task_id)
        if task.state in {TaskStatus.COMPLETED, TaskStatus.PR_READY, TaskStatus.USER_REVIEW}:
            return {"task_id": task_id, "state": task.state.value}
        snapshot = self.store.snapshot(task_id)
        budget = limits(snapshot["resolved"].get("execution_limits"))
        max_reentries = (
            budget["max_reentries"]
            if max_reentries is None
            else min(max_reentries, budget["max_reentries"])
        )
        if not self.store.approved(task_id):
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Current approvals required.")
        context = TransitionContext(
            design_approved=True,
            plan_approved=True,
            snapshot_created=True,
            dependencies_completed=self.store.dependencies_complete(task_id),
            preflight_passed=self.catalog.preflight(task),
            lease_acquired=True,
            expected_version=task.version,
        )
        policy = snapshot["resolved"].get("usage_policy")
        budget_pause = getattr(self.store, "budget_pause", None)
        if task.state == TaskStatus.BLOCKED and budget_pause:
            due, summary = budget_pause(task_id, policy)
            if due:
                approve = getattr(self.store, "approve_budget_pause", None)
                if approve:
                    approve(task_id, summary)  # Resuming is the approval.
        if task.state in {TaskStatus.BLOCKED, TaskStatus.WAITING_FOR_CAPACITY}:
            task = self.store.transition(task_id, TaskStatus.READY, context, lease)
            context = replace(context, expected_version=task.version)
        if task.state == TaskStatus.READY:
            task = self.store.transition(task_id, TaskStatus.RUNNING, context, lease)
        elif task.state == TaskStatus.VERIFYING:
            return {"task_id": task_id, "state": "verifying", "snapshot_digest": snapshot["digest"]}
        elif task.state != TaskStatus.RUNNING:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Task is not executable.")

        self.store.recover_attempts(task_id, lease)
        phases = self.catalog.phases(task)
        generation = self.store.generation(task_id)
        index = 0
        while index < len(phases):
            phase_id, capabilities = phases[index]
            if self.store.passed_attempt(task_id, phase_id, generation):
                index += 1
                continue
            usage = self.store.execution_usage(task_id)
            remaining = budget["max_runner_seconds"] - usage["runner_seconds"]
            if usage["attempts"] >= budget["max_attempts"] or remaining < 1:
                self.store.event(
                    task_id, "ExecutionLimitReached", {"limits": budget, "usage": usage}
                )
                current = self.store.task(task_id)
                self.store.transition(
                    task_id,
                    TaskStatus.BLOCKED,
                    TransitionContext(expected_version=current.version),
                    lease,
                )
                return {
                    "task_id": task_id,
                    "state": "blocked",
                    "error": ErrorCode.POLICY_VIOLATION.value,
                    "reason": "EXECUTION_LIMIT_REACHED",
                    "usage": usage,
                }
            recover = getattr(self.catalog, "recover_interrupted", None)
            try:
                if recover:
                    recover(task_id, phase_id)  # A crashed attempt's leftovers are checked first.
            except DomainError as exc:
                self.store.event(
                    task_id, "ExecutionBlocked", {"code": exc.code.value, "reason": str(exc), "phase_id": phase_id}
                )
                current = self.store.task(task_id)
                self.store.transition(
                    task_id, TaskStatus.BLOCKED, TransitionContext(expected_version=current.version), lease
                )
                return {"task_id": task_id, "state": "blocked", "error": exc.code.value, "reason": str(exc)}
            if budget_pause and budget_pause(task_id, policy)[0]:
                self.store.event(
                    task_id,
                    "ExecutionBlocked",
                    {"code": ErrorCode.POLICY_VIOLATION.value, "reason": "BUDGET_PAUSE"},
                )
                current = self.store.task(task_id)
                self.store.transition(
                    task_id,
                    TaskStatus.BLOCKED,
                    TransitionContext(expected_version=current.version),
                    lease,
                )
                return {
                    "task_id": task_id,
                    "state": "blocked",
                    "error": ErrorCode.POLICY_VIOLATION.value,
                    "reason": "BUDGET_PAUSE",
                }
            key = digest([task_id, phase_id, snapshot["digest"], generation])
            attempt = self.store.begin_attempt(task_id, phase_id, key, lease)
            if attempt["state"] == "passed":
                index += 1
                continue
            started = time.monotonic()
            result = None
            request = None
            try:
                role_context = json.loads(self.artifacts.context(task_id, phase_id))
                role_context["role_policy"] = (
                    snapshot["resolved"].get("role_policies", {}).get(phase_id, {})
                )
                role_context["node_config"] = next(
                    (
                        n
                        for n in snapshot["resolved"].get("workflow_graph", {}).get("nodes", [])
                        if n["id"] == phase_id
                    ),
                    {},
                )
                request = PhaseRequest(
                    task,
                    phase_id,
                    attempt["id"],
                    snapshot["digest"],
                    generation,
                    capabilities,
                    json.dumps(role_context, ensure_ascii=False),
                    timeout_seconds=max(1, min(budget["phase_timeout_seconds"], int(remaining))),
                )
                self.artifacts.prompt(task_id, phase_id, attempt["id"], request.context)
                capture = getattr(self.catalog, "role_before", None)
                before = capture(task_id, phase_id) if capture else None
                save_baseline = getattr(self.catalog, "save_baseline", None)
                if save_baseline and before is not None:
                    save_baseline(task_id, phase_id, attempt["id"], before)
                result = self.runners.execute(request)
                self.artifacts.write(task_id, phase_id, attempt["id"], result.output)
                from .workflow_dag import parse_with_repair

                result, decision = parse_with_repair(
                    self.parser,
                    {p[0] for p in phases},
                    request,
                    result,
                    self.runners,
                    capture=(lambda c=capture, p=phase_id: c(task_id, p)) if capture else None,
                    seconds_left=remaining - (time.monotonic() - started),
                    record=lambda a=attempt["id"], p=phase_id: self.store.event(
                        task_id, "ControlRepairRequested", {"attempt_id": a, "phase_id": p}
                    ),
                )
                apply_result = getattr(self.catalog, "apply_role_result", None)
                if apply_result:
                    apply_result(
                        task_id,
                        phase_id,
                        result.output if decision["handoff_status"] == "pass" else "",
                        before,
                        lease,
                        generation,
                    )
                if self.hooks and decision["handoff_status"] == "pass" and "code.write" in capabilities:
                    # after_edit hooks (lint, format checks): a failure sends the phase back.
                    failed = next(
                        (r for r in self.hooks(task_id, "after_edit", {"phase": phase_id}) if not r["ok"]),
                        None,
                    )
                    if failed:
                        decision = {
                            "handoff_status": "issue",
                            "issue_type": "implementation",
                            "root_cause": f"after_edit 훅 '{failed['name']}' 실패 (종료 코드 {failed['exit_code']}): "
                            + failed["output"][-1500:],
                            "owning_agent_id": phase_id,
                            "reentry_step_id": phase_id,
                        }
            except DomainError as exc:
                self.store.event(
                    task_id,
                    "ExecutionBlocked",
                    {"phase_id": phase_id, "code": exc.code.value, "reason": str(exc)},
                )
                state = (
                    "waiting_for_capacity" if exc.code == ErrorCode.CAPACITY_EXHAUSTED else "failed"
                )
                self.store.finish_attempt(attempt["id"], state, lease)
                current = self.store.task(task_id)
                target = (
                    TaskStatus.WAITING_FOR_CAPACITY
                    if state == "waiting_for_capacity"
                    else TaskStatus.BLOCKED
                )
                self.store.transition(
                    task_id,
                    target,
                    TransitionContext(capacity_exhausted=True, expected_version=current.version),
                    lease,
                )
                return {
                    "task_id": task_id,
                    "state": target.value,
                    "error": exc.code.value,
                    "reason": str(exc),
                }
            except BaseException as exc:
                self.store.finish_attempt(attempt["id"], "interrupted", lease)
                current = self.store.task(task_id)
                self.store.transition(
                    task_id,
                    TaskStatus.BLOCKED,
                    TransitionContext(expected_version=current.version),
                    lease,
                )
                if not isinstance(exc, Exception):
                    raise
                return {
                    "task_id": task_id,
                    "state": "blocked",
                    "error": ErrorCode.EXTERNAL_TRANSIENT.value,
                    "reason": "EXECUTION",
                }
            finally:
                self.store.event(
                    task_id,
                    "RunnerUsage",
                    {
                        "attempt_id": attempt["id"],
                        "phase_id": phase_id,
                        "runner_seconds": round(time.monotonic() - started, 6),
                        "provider": result.provider if result else "",
                        "usage": safe_usage(result.usage) if result else {},
                        "selection": result.selection if result else {},
                        "context_chars": context_profile(request.context) if request else None,
                    },
                )
                record_thresholds = getattr(self.store, "record_usage_thresholds", None)
                if record_thresholds:
                    record_thresholds(task_id, snapshot["resolved"].get("usage_policy"))
            if decision["handoff_status"] == "pass":
                self.store.finish_attempt(attempt["id"], "passed", lease)
                index += 1
                continue
            self.store.finish_attempt(attempt["id"], "failed", lease)
            owner = decision.get("reentry_step_id", "")
            if owner not in {p[0] for p in phases}:
                current = self.store.task(task_id)
                self.store.transition(
                    task_id,
                    TaskStatus.BLOCKED,
                    TransitionContext(expected_version=current.version),
                    lease,
                )
                return {
                    "task_id": task_id,
                    "state": "blocked",
                    "error": ErrorCode.POLICY_VIOLATION.value,
                    "reason": "OPERATOR_ACTION_REQUIRED",
                }
            reentry_index = next(i for i, phase in enumerate(phases) if phase[0] == owner)
            fingerprint = digest(
                [task_id, owner, decision.get("issue_type"), decision.get("root_cause")]
            )
            self.store.event(
                task_id, "FindingDetected", {"fingerprint": fingerprint, "decision": decision}
            )
            try:
                generation = self.store.reenter(
                    task_id,
                    [p[0] for p in phases[reentry_index:]],
                    fingerprint,
                    max_reentries,
                    lease,
                )
            except DomainError:
                current = self.store.task(task_id)
                self.store.transition(
                    task_id,
                    TaskStatus.BLOCKED,
                    TransitionContext(expected_version=current.version),
                    lease,
                )
                return {
                    "task_id": task_id,
                    "state": "blocked",
                    "error": ErrorCode.POLICY_VIOLATION.value,
                    "reason": "REENTRY_EXHAUSTED",
                }
            index = reentry_index
        current = self.store.task(task_id)
        self.store.transition(
            task_id,
            TaskStatus.VERIFYING,
            TransitionContext(phases_passed=True, expected_version=current.version),
            lease,
        )
        return {"task_id": task_id, "state": "verifying", "snapshot_digest": snapshot["digest"]}
