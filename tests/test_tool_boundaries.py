import sys
import time

import pytest

from agent_dev.adapters.external_tools import ProcessOperation
from agent_dev.adapters.runner_registry import CapabilityRunnerRegistry, RunnerCandidate
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.tool_gateway import ToolGateway
from agent_dev.domain.contracts import PhaseRequest, PhaseResult, TaskRecord, ToolConnection
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.status import TaskStatus


@pytest.fixture
def gateway(tmp_path):
    repository = WorkflowRepository(Database(tmp_path / "state.db"), secrets=("private-value",))
    repository.register_project("P", "Project", {})
    design = "## Scope\nDesign\n## Acceptance\nPass\n## Constraints\nExplicit tools only"
    repository.design("P", "D", design)
    repository.approve("design", "D", 1, "user")
    repository.create_plan("P", "PLAN", "D", 1, [{"id": "T", "title": "Design task"}])
    repository.approve("plan", "PLAN", 1, "user")
    repository.create_snapshot("T", {"design": design, "phases": [{"id": "designer"}]})
    return ToolGateway(repository)


def connection(capabilities=("design.read",), allowlist=("scope",), **changes):
    values = {
        "connection_id": "tool",
        "project_id": "P",
        "capabilities": frozenset(capabilities),
        "enabled": True,
        "authenticated": True,
        "healthy": True,
        "allowlist": allowlist,
    }
    return ToolConnection(**dict(values, **changes))


class ResultOperation:
    def execute(self, timeout_seconds):
        return {"token": "private-value", "text": "private-value"}


def test_grant_consumed_before_call_and_scope_bound(gateway):
    tool = connection(("design.write",), ("scope", "different"))
    gateway.register(tool)
    approval = gateway.grant(
        "tool", "design.write", "scope", "user", task_id="T", phase_id="designer"
    )
    with pytest.raises(DomainError):
        gateway.call(
            tool,
            "design.write",
            "different",
            ResultOperation(),
            approval_id=approval,
            task_id="T",
            phase_id="designer",
        )
    result = gateway.call(
        tool,
        "design.write",
        "scope",
        ResultOperation(),
        approval_id=approval,
        task_id="T",
        phase_id="designer",
    )
    assert "private-value" not in str(result)
    with pytest.raises(DomainError) as error:
        gateway.call(
            tool,
            "design.write",
            "scope",
            ResultOperation(),
            approval_id=approval,
            task_id="T",
            phase_id="designer",
        )
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED
    assert gateway.repository.db.rows("SELECT * FROM events WHERE kind='ToolCall'")[0][
        "payload_json"
    ]


@pytest.mark.parametrize(
    "change,code",
    [
        ({"enabled": False}, ErrorCode.CAPABILITY_UNAVAILABLE),
        ({"healthy": False}, ErrorCode.CAPABILITY_UNAVAILABLE),
        ({"authenticated": False}, ErrorCode.AUTH_REQUIRED),
    ],
)
def test_ineligible_tools_do_not_dispatch(gateway, change, code):
    tool = connection(**change)
    gateway.register(tool)
    with pytest.raises(DomainError) as error:
        gateway.call(tool, "design.read", "scope", ResultOperation())
    assert error.value.code == code


def test_process_timeout_kills_descendants(tmp_path):
    marker = tmp_path / "late"
    script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]);time.sleep(10)"
    child = f"import time;from pathlib import Path;time.sleep(2);Path({str(marker)!r}).write_text('unsafe')"
    operation = ProcessOperation([sys.executable, "-c", script, child], tmp_path)
    with pytest.raises(DomainError) as error:
        operation.execute(1)
    assert error.value.code == ErrorCode.EXTERNAL_TRANSIENT
    time.sleep(1.2)
    assert not marker.exists()


class Runner:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def execute(self, request):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return PhaseResult(self.result)


def test_capacity_failover_persists_cooldown(gateway):
    safety = ExecutionSafety(gateway.repository)
    first = Runner(DomainError(ErrorCode.CAPACITY_EXHAUSTED, "capacity"))
    second = Runner("ok")
    registry = CapabilityRunnerRegistry(
        [
            RunnerCandidate(connection(("code.read",)), first),
            RunnerCandidate(connection(("code.read",), connection_id="backup"), second),
        ],
        safety,
    )
    request = PhaseRequest(
        TaskRecord("T", "P", TaskStatus.RUNNING, 1, {}, "s", "plan"),
        "phase",
        "attempt",
        "digest",
        0,
        frozenset({"code.read"}),
        "",
    )
    assert registry.execute(request).output == "ok"
    assert registry.execute(request).output == "ok"
    assert first.calls == 1
    assert second.calls == 2
    assert not ExecutionSafety(gateway.repository).available("tool")


def test_conversation_owner_runs_first(gateway):
    claude, codex = Runner("claude"), Runner("codex")
    claude.provider, codex.provider = "claude_cli", "codex_cli"
    registry = CapabilityRunnerRegistry(
        [
            RunnerCandidate(connection(("code.read",)), claude),
            RunnerCandidate(connection(("code.read",), connection_id="backup"), codex),
        ],
        ExecutionSafety(gateway.repository),
    )

    def request(token):
        return PhaseRequest(
            TaskRecord("T", "P", TaskStatus.RUNNING, 1, {}, "s", "plan"),
            "phase",
            "attempt",
            "digest",
            1,
            frozenset({"code.read"}),
            "",
            resume_session=token,
        )

    assert registry.execute(request("codex_cli:t-1")).output == "codex"
    assert registry.execute(request("claude_cli:s-1")).output == "claude"
    assert registry.execute(request("")).output == "claude"


def test_unknown_capability_does_not_spend_capacity(gateway):
    runner = Runner("ok")
    registry = CapabilityRunnerRegistry(
        [RunnerCandidate(connection(), runner)], ExecutionSafety(gateway.repository)
    )
    request = PhaseRequest(
        TaskRecord("T", "P", TaskStatus.RUNNING, 1, {}, None, None),
        "phase",
        "attempt",
        "digest",
        0,
        frozenset({"code.write"}),
        "",
    )
    with pytest.raises(DomainError) as error:
        registry.execute(request)
    assert error.value.code == ErrorCode.CAPABILITY_UNAVAILABLE
    assert runner.calls == 0
