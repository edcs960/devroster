import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_dev.adapters.artifacts import FilesystemArtifacts
from agent_dev.adapters.harness import parse_harness
from agent_dev.adapters.phase_catalog import SnapshotPhaseCatalog
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.verification import VerificationAdapter
from agent_dev.application.workflow import WorkflowCoordinator
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext


@pytest.fixture
def repository(tmp_path):
    repo = WorkflowRepository(Database(tmp_path / "state.db"), secrets=("test-private-secret",))
    repo.register_project("p", "Project", {"token": "test-private-secret"})
    repo.design(
        "p",
        "design",
        "## Scope\nApproved design\n## Acceptance\nTests pass\n## Constraints\nApproved paths only",
    )
    repo.approve("design", "design", 1, "user")
    repo.create_plan("p", "plan", "design", 1, [{"id": "T1", "title": "Implement"}])
    repo.approve("plan", "plan", 1, "user")
    subprocess.run(["git", "init", "-q", str(tmp_path / "worktree")], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path / "worktree"),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
    )
    repo.create_snapshot(
        "T1",
        {
            "design": "## Scope\nApproved design\n## Acceptance\nTests pass\n## Constraints\nApproved paths only",
            "repositories": {"source": {"worktree": str(tmp_path / "worktree")}},
            "phases": [{"id": "developer", "capabilities": ["code.write"]}],
            "verification": {"required_gates": ["unit"], "commands": []},
        },
    )
    return repo


def test_schema_snapshot_approval(repository):
    connection = repository.db.connect()
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    connection.close()
    assert "test-private-secret" not in repository.db.path.read_bytes().decode(errors="ignore")
    with pytest.raises(sqlite3.IntegrityError), repository.db.transaction() as connection:
        connection.execute("UPDATE input_snapshots SET resolved_json='{}'")
    repository.design("p", "design", "Revised design")
    assert not repository.approved("T1")
    safety = ExecutionSafety(repository)
    with safety.lease("task:T1") as lease, pytest.raises(DomainError) as error:
        repository.transition(
            "T1", TaskStatus.RUNNING, TransitionContext(expected_version=1), lease
        )
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED


def test_concurrent_lease_fencing_and_recovery(repository):
    safety = ExecutionSafety(repository)

    def acquire(holder):
        try:
            return safety.acquire("task:T1", holder)
        except DomainError:
            return None

    with ThreadPoolExecutor(2) as pool:
        grants = list(pool.map(acquire, ["one", "two"]))
    assert sum(g is not None for g in grants) == 1
    old = next(g for g in grants if g)
    repository.transition(
        "T1",
        TaskStatus.RUNNING,
        TransitionContext(
            expected_version=1,
            dependencies_completed=True,
            preflight_passed=True,
            lease_acquired=True,
        ),
        old,
    )
    first = repository.begin_attempt("T1", "developer", "phase-key", old)
    safety.release(old)
    new = safety.acquire("task:T1", "new")
    assert new["token"] > old["token"]
    with pytest.raises(DomainError):
        repository.finish_attempt(first["id"], "passed", old)
    second = repository.begin_attempt("T1", "developer", "phase-key", new)
    assert second["attempt_no"] == 2
    assert (
        repository.db.rows("SELECT state FROM phase_attempts WHERE id=?", (first["id"],))[0][
            "state"
        ]
        == "interrupted"
    )
    repository.finish_attempt(second["id"], "passed", new)
    assert repository.begin_attempt("T1", "developer", "phase-key", new)["id"] == second["id"]


def test_command_outbox_and_versions(repository):
    safety = ExecutionSafety(repository)
    calls = []
    for _ in range(2):
        assert safety.command("k", {"x": 1}, lambda: calls.append(1) or {"ok": True}) == {
            "ok": True
        }
    assert calls == [1]
    with pytest.raises(DomainError):
        safety.command("k", {"x": 2}, lambda: None)
    deliveries = []
    assert safety.deliver(lambda key, value: deliveries.append(key))["delivered"] > 0
    assert safety.deliver(lambda key, value: deliveries.append(key))["delivered"] == 0
    assert len(deliveries) == len(set(deliveries))
    assert repository.db.rows("SELECT COUNT(*) AS n FROM events")[0]["n"] == len(deliveries)
    with safety.lease("task:T1") as lease, pytest.raises(DomainError) as error:
        repository.transition(
            "T1", TaskStatus.RUNNING, TransitionContext(expected_version=0), lease
        )
    assert error.value.code == ErrorCode.STALE_VERSION


class FakeRunner:
    def __init__(self, outputs):
        self.outputs, self.calls = iter(outputs), 0

    def execute(self, request):
        self.calls += 1
        result = next(self.outputs)
        if isinstance(result, Exception):
            raise result
        return PhaseResult(result)


PASS = '```json\n{"handoff_status":"pass"}\n```'


def coordinator(repository, tmp_path, outputs):
    runner = FakeRunner(outputs)
    return WorkflowCoordinator(
        repository,
        runner,
        FilesystemArtifacts(repository, tmp_path / "output"),
        SnapshotPhaseCatalog(repository, tmp_path),
        parse_harness,
    ), runner


def test_complete_phases_resume_and_required_verification(repository, tmp_path):
    flow, runner = coordinator(repository, tmp_path, [PASS])
    safety = ExecutionSafety(repository)
    with safety.lease("task:T1") as lease:
        assert flow.execute("T1", lease)["state"] == "verifying"
        assert flow.execute("T1", lease)["state"] == "verifying"
        assert runner.calls == 1
        result = VerificationAdapter(repository).verify("T1", lease)
        assert result["passed"] is False
        assert result["state"] == "blocked"
    artifact = repository.db.rows("SELECT * FROM artifacts WHERE kind='phase_output'")[0]
    assert artifact["trust"] == "untrusted"
    assert artifact["attempt_id"]


@pytest.mark.parametrize(
    "output",
    ["", '```json\n{"handoff_status":"pass"}\n```\n```json\n{"handoff_status":"pass"}\n```'],
)
def test_bad_output_preserved_and_blocked(repository, tmp_path, output):
    flow, _ = coordinator(repository, tmp_path, [output])
    with ExecutionSafety(repository).lease("task:T1") as lease:
        assert flow.execute("T1", lease)["state"] == "blocked"
    assert len(repository.db.rows("SELECT * FROM artifacts WHERE kind='phase_output'")) == 1


def test_capacity_resume_creates_attempt(repository, tmp_path):
    flow, runner = coordinator(
        repository, tmp_path, [DomainError(ErrorCode.CAPACITY_EXHAUSTED, "quota"), PASS]
    )
    safety = ExecutionSafety(repository)
    with safety.lease("task:T1") as lease:
        assert flow.execute("T1", lease)["state"] == "waiting_for_capacity"
    with safety.lease("task:T1") as lease:
        assert flow.execute("T1", lease)["state"] == "verifying"
    assert runner.calls == 2
    assert [
        r["state"] for r in repository.db.rows("SELECT * FROM phase_attempts ORDER BY attempt_no")
    ] == ["waiting_for_capacity", "passed"]


def test_bounded_reentry(repository, tmp_path):
    issue = '```json\n{"handoff_status":"issue","issue_type":"bug","root_cause":"bad result","reentry_step_id":"developer"}\n```'
    flow, runner = coordinator(repository, tmp_path, [issue, issue])
    with ExecutionSafety(repository).lease("task:T1") as lease:
        assert flow.execute("T1", lease)["reason"] == "REENTRY_EXHAUSTED"
    assert runner.calls == 2
    assert repository.db.rows("SELECT * FROM findings")[0]["retries"] == 1
