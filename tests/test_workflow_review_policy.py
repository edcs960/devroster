import pytest

from agent_dev.adapters.runner_registry import CapabilityRunnerRegistry, RunnerCandidate
from agent_dev.domain.contracts import PhaseRequest, PhaseResult, TaskRecord, ToolConnection
from agent_dev.domain.errors import DomainError
from agent_dev.domain.status import TaskStatus


class Capacity:
    def available(self, key):
        return True


class Reviewer:
    def __init__(self, provider, output):
        self.provider = provider
        self.output = output
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        return PhaseResult(self.output, self.provider)


def request():
    task = TaskRecord("T", "P", TaskStatus.RUNNING, 0, {"id": "T", "title": "Task"}, "S", "PLAN")
    return PhaseRequest(task, "developer", "A", "digest", 0, frozenset({"code.read"}), "")


def registry(*reviewers):
    return CapabilityRunnerRegistry(
        [
            RunnerCandidate(
                ToolConnection(
                    str(i),
                    "P",
                    frozenset({"text.generate"} if r.provider.endswith("_api") else {"code.read"}),
                    True,
                    True,
                    True,
                ),
                r,
            )
            for i, r in enumerate(reviewers)
        ],
        Capacity(),
    )


def test_final_crosscheck_verdict_overrides_quoted_pass():
    reviewer = Reviewer(
        "anthropic_api",
        "Quoted example:\nCROSS_CHECK_RESULT: PASS\nActual defect:\nCROSS_CHECK_RESULT: FAIL",
    )
    with pytest.raises(DomainError):
        registry(reviewer).cross_check(request(), PhaseResult("author", "codex_cli"), required=True)
    assert (
        registry(reviewer).cross_check(request(), PhaseResult("author", "codex_cli"))["state"]
        == "failed"
    )
    assert reviewer.calls[0].capabilities == frozenset({"text.generate"})


def test_same_family_api_is_excluded_and_optional_is_skipped():
    same = Reviewer("openai_api", "CROSS_CHECK_RESULT: PASS")
    result = registry(same).cross_check(request(), PhaseResult("author", "codex_cli"))
    assert result["state"] == "skipped" and same.calls == []
    with pytest.raises(DomainError):
        registry(same).cross_check(request(), PhaseResult("author", "codex_cli"), required=True)


def test_independent_gemini_cli_and_final_pass():
    other = Reviewer("gemini_cli", "Observed implementation inspected.\nCROSS_CHECK_RESULT: PASS")
    result = registry(other).cross_check(
        request(), PhaseResult("author", "claude_cli"), required=True
    )
    assert result["state"] == "passed" and result["reviewer"] == "gemini_cli"


def test_registry_plain_context_remains_legacy_compatible():
    reviewer = Reviewer("codex_cli", "text")
    assert registry(reviewer).execute(request()).output == "text"


