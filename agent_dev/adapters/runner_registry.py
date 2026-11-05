"""Capabilities and persistent capacity choose candidates; providers stay inside adapters."""

import time
from dataclasses import dataclass

from agent_dev.domain.contracts import PhaseRequest, PhaseResult, ToolConnection
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ports.workflow import RunnerPort


@dataclass(frozen=True)
class RunnerCandidate:
    connection: ToolConnection
    runner: RunnerPort


class CapabilityRunnerRegistry:
    def __init__(self, candidates: list[RunnerCandidate], safety, cooldown_seconds: int = 1800):
        self.candidates, self.safety = candidates, safety
        self.cooldown_seconds = cooldown_seconds

    def execute(self, request: PhaseRequest) -> PhaseResult:
        import json

        try:
            context = json.loads(request.context)
        except (ValueError, TypeError):
            context = {}
        node = context.get("node_config", {}) if isinstance(context, dict) else {}
        fixed_provider = node.get("provider")
        candidates = [
            candidate
            for candidate in self.candidates
            if (not fixed_provider or getattr(candidate.runner, "provider", None) == fixed_provider)
            and candidate.connection.project_id == request.task.project_id
            and candidate.connection.enabled
            and candidate.connection.healthy
            and request.capabilities <= candidate.connection.capabilities
        ]
        if not candidates:
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE, "Required runner capabilities unavailable."
            )
        candidates = [candidate for candidate in candidates if candidate.connection.authenticated]
        if not candidates:
            raise DomainError(ErrorCode.AUTH_REQUIRED, "Eligible runner requires authentication.")
        # The provider that owns the conversation goes first so the session can continue;
        # the others keep their order and start fresh if it is unavailable.
        owner = (getattr(request, "resume_session", "") or "").partition(":")[0]
        if owner:
            candidates.sort(key=lambda c: getattr(c.runner, "provider", None) != owner)
        for candidate in candidates:
            if not self.safety.available(candidate.connection.connection_id):
                continue
            for retry in range(2):
                try:
                    return self._execute_node(candidate.runner, request, node)
                except DomainError as exc:
                    if exc.code == ErrorCode.EXTERNAL_TRANSIENT and retry == 0:
                        time.sleep(0.1)
                        continue
                    if exc.code != ErrorCode.CAPACITY_EXHAUSTED:
                        raise
                    self.safety.cooldown(candidate.connection.connection_id, self.cooldown_seconds)
                    self.safety.repository.event(
                        request.task.task_id,
                        "RunnerCapacityWait",
                        {"connection_id": candidate.connection.connection_id},
                    )
                    break
        raise DomainError(ErrorCode.CAPACITY_EXHAUSTED, "All eligible runners are on cooldown.")

    def cross_check(self, request, result, *, required=False):
        import json
        import re
        from dataclasses import replace

        from agent_dev.domain.content import digest
        from agent_dev.domain.role_policy import independent_family

        candidates = [
            c
            for c in self.candidates
            if c.connection.project_id == request.task.project_id
            and c.connection.enabled
            and c.connection.healthy
            and c.connection.authenticated
            and bool({"code.read", "text.generate"} & c.connection.capabilities)
            and bool(getattr(c.runner, "provider", ""))
            and independent_family(getattr(c.runner, "provider", ""))
            != independent_family(result.provider)
        ]
        for candidate in candidates:
            if not self.safety.available(candidate.connection.connection_id):
                continue
            reviewed = {
                "instruction": "Independent read-only review. Return CROSS_CHECK_RESULT: PASS or FAIL.",
                "artifact_digest": digest(result.output),
                "artifact_untrusted": result.output[-20000:],
                "repositories": {},
                "role_policy": {"mode": "readonly", "scopes": []},
            }
            review = replace(
                request,
                capabilities=frozenset(
                    {
                        "text.generate"
                        if "text.generate" in candidate.connection.capabilities
                        else "code.read"
                    }
                ),
                context=json.dumps(reviewed),
            )
            try:
                response = candidate.runner.execute(review)
            except DomainError as exc:
                if required:
                    raise
                return {
                    "state": "error",
                    "reason": exc.code.value,
                    "author": result.provider,
                    "reviewer": getattr(candidate.runner, "provider", ""),
                }
            lines = [line.strip() for line in response.output.splitlines() if line.strip()]
            verdict = re.fullmatch(
                r"CROSS_CHECK_RESULT\s*:\s*(PASS|FAIL)", lines[-1] if lines else "", re.IGNORECASE
            )
            if verdict and verdict.group(1).upper() == "PASS":
                return {
                    "state": "passed",
                    "artifact_digest": digest(result.output),
                    "author": result.provider,
                    "reviewer": response.provider,
                    "selection": response.selection,
                    "reason": "Different registered provider family",
                    "review_digest": digest(response.output),
                    "review_usage": response.usage,
                    "review_output_untrusted": response.output[-20000:],
                }
            if required:
                raise DomainError(
                    ErrorCode.VERIFICATION_FAILED, "Required independent review did not pass."
                )
            return {
                "state": "failed",
                "artifact_digest": digest(result.output),
                "reviewer": response.provider,
                "verdict": verdict.group(1).upper() if verdict else "UNKNOWN",
            }
        if required:
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Required independent registered reviewer unavailable.",
            )
        return {"state": "skipped", "reason": "INDEPENDENT_REVIEW_UNAVAILABLE"}

    @staticmethod
    def _execute_node(adapter, request, node):
        from copy import deepcopy

        holder = getattr(adapter, "runner", adapter)
        attribute = (
            "model_config"
            if hasattr(holder, "model_config")
            else "config"
            if hasattr(holder, "config")
            else ""
        )
        if not attribute or not any(
            node.get(k) not in {None, "", "auto"} for k in ("model", "effort")
        ):
            return adapter.execute(request)
        original = getattr(holder, attribute)
        override = deepcopy(original)
        override["routing"] = {"mode": "fixed"}
        for key in ("model", "effort"):
            if node.get(key) not in {None, "", "auto"}:
                override[key] = node[key]
        setattr(holder, attribute, override)
        try:
            return adapter.execute(request)
        finally:
            setattr(holder, attribute, original)
