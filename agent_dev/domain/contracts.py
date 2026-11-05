"""Provider-independent command, phase and evidence contracts."""

from dataclasses import dataclass, field
from typing import Any

from agent_dev.domain.status import TaskStatus


@dataclass(frozen=True)
class Command:
    request_id: str
    operation: str
    actor: str
    project_id: str = ""
    task_id: str = ""
    expected_version: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    project_id: str
    state: TaskStatus
    version: int
    definition: dict[str, Any]
    snapshot_id: str | None
    plan_id: str | None


@dataclass(frozen=True)
class PhaseRequest:
    task: TaskRecord
    phase_id: str
    attempt_id: str
    snapshot_digest: str
    generation: int
    capabilities: frozenset[str]
    context: str
    timeout_seconds: int = 1800
    # Session nodes served by this one invocation (ADR 0002); empty for a single role node.
    node_ids: tuple[str, ...] = ()
    # "<provider>:<session id>" of an earlier invocation to continue (ADR 0002 W4).
    resume_session: str = ""


@dataclass(frozen=True)
class PhaseResult:
    output: str
    provider: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    selection: dict[str, Any] = field(default_factory=dict)
    # "<provider>:<session id>" a later invocation may resume; empty when unavailable.
    resume_session: str = ""


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    required: bool = True
    exit_code: int | None = None
    evidence: str = ""


@dataclass(frozen=True)
class ToolConnection:
    connection_id: str
    project_id: str
    capabilities: frozenset[str]
    enabled: bool
    authenticated: bool
    healthy: bool
    allowlist: tuple[str, ...] = ()
    credential_ref: str = ""


def aggregate_gates(results: list[GateResult]) -> bool:
    required = [result for result in results if result.required]
    return bool(required) and all(result.passed for result in required)
