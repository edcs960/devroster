import base64
import http.client
import json
import threading

import pytest
from test_reusable_workspace import approved
from test_reusable_workspace import workspace as shared_workspace

from agent_dev.adapters import upgrades
from agent_dev.adapters.command_line import main
from agent_dev.adapters.presets import listing, profile, select, write
from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.content import content_digest
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.execution_limits import limits, safe_usage
from agent_dev.runners.usage import claude_result, codex_usage
from agent_dev.ui.dashboard import make_server


@pytest.fixture(name="workspace")
def reusable_fixture(tmp_path):
    return shared_workspace.__wrapped__(tmp_path)


@pytest.mark.parametrize(
    "name", ["generic", "python", "react", "spring-boot", "spring-boot-gradle"]
)
def test_presets_have_required_gates_and_isolated_roles(tmp_path, name):
    value = profile(name)
    assert value["verification"]["required_gates"]
    assert all(
        item["required"] and isinstance(item["command"], list)
        for item in value["verification"]["commands"]
    )
    initialize(tmp_path / name, preset=name)
    saved = json.loads((tmp_path / name / ".agent-dev/config/profile.json").read_text(encoding="utf-8"))
    assert saved["verification"] == value["verification"]
    assert saved["allowed_paths"] == value["allowed_paths"]
    assert not saved["runners"][0]["authenticated"]


def test_preset_inference_is_explicit_and_no_overwrite(tmp_path):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert select("auto", tmp_path) == "python"
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DomainError):
        select("auto", tmp_path)
    destination = tmp_path / "profile.json"
    write("react", destination)
    before = destination.read_bytes()
    with pytest.raises(DomainError):
        write("python", destination)
    assert destination.read_bytes() == before
    assert len(listing()["presets"]) == 5
    gradle = tmp_path / "api"
    gradle.mkdir()
    (gradle / "build.gradle.kts").write_text("", encoding="utf-8")
    assert select("auto", gradle) == "spring-boot-gradle"


@pytest.mark.parametrize(
    "value",
    [
        {"max_attempts": True},
        {"max_runner_seconds": 0},
        {"phase_timeout_seconds": 7201},
        {"max_reentries": -1},
        {"unknown": 1},
        [],
    ],
)
def test_execution_limit_validation(value):
    with pytest.raises(DomainError):
        limits(value)


def old_workspace(root):
    managed = root / ".agent-dev"
    marker = json.loads((managed / "workspace.json").read_text(encoding="utf-8"))
    marker.pop("engine_version", None)
    (managed / "workspace.json").write_text(json.dumps(marker), encoding="utf-8")
    value = json.loads((managed / "config/profile.json").read_text(encoding="utf-8"))
    value.pop("execution_limits", None)
    (managed / "config/profile.json").write_text(json.dumps(value), encoding="utf-8")
    runtime = WorkflowRuntime(root)
    version = runtime.store.db.rows("SELECT version FROM projects WHERE id='EXAMPLE'")[0]["version"]
    runtime.store.revise_profile("EXAMPLE", value, version)
    return runtime


def test_upgrade_dryrun_backup_apply_idempotency_and_snapshot_preservation(workspace, tmp_path):
    runtime = approved(workspace)
    snapshot = runtime.store.snapshot("T")
    old_workspace(workspace)
    before = (workspace / ".agent-dev/state.db").read_bytes()
    report = upgrades.upgrade(workspace)
    assert report["from_version"] == "0.2.0" and not report["applied"]
    assert before == (workspace / ".agent-dev/state.db").read_bytes()
    archive = tmp_path / "upgrade.zip"
    result = upgrades.upgrade(workspace, apply=True, backup=archive)
    assert result["applied"] and archive.is_file()
    assert runtime.store.snapshot("T") == snapshot
    assert runtime.store.approved("T")
    assert not upgrades.upgrade(workspace, apply=True)["applied"]
    marker = json.loads((workspace / ".agent-dev/workspace.json").read_text(encoding="utf-8"))
    assert marker["engine_version"] == "0.3.0"
    rows = runtime.store.db.rows(
        "SELECT content_json FROM project_profile_versions WHERE project_id='EXAMPLE' ORDER BY version DESC LIMIT 1"
    )
    assert json.loads(rows[0]["content_json"])["execution_limits"] == limits()


def test_upgrade_rolls_back_config_and_database_on_failure(workspace, tmp_path, monkeypatch):
    runtime = old_workspace(workspace)
    before = upgrades.inputs(workspace / ".agent-dev")
    profiles = runtime.store.db.rows("SELECT * FROM project_profile_versions")
    original = upgrades.atomic
    calls = 0

    def fail_once(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture simulated replace failure")
        original(path, payload)

    monkeypatch.setattr(upgrades, "atomic", fail_once)
    with pytest.raises(OSError):
        upgrades.upgrade(workspace, apply=True, backup=tmp_path / "backup.zip")
    assert upgrades.inputs(workspace / ".agent-dev") == before
    assert runtime.store.db.rows("SELECT * FROM project_profile_versions") == profiles
    assert not (workspace / ".agent-dev/upgrade-journal.json").exists()


def test_upgrade_interrupted_journal_recovers_and_blocks_execution(
    workspace, tmp_path, monkeypatch
):
    old_workspace(workspace)
    before = upgrades.inputs(workspace / ".agent-dev")
    original = upgrades.atomic
    calls = 0

    def interrupt(path, payload):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("fixture interrupted recovery")
        original(path, payload)

    monkeypatch.setattr(upgrades, "atomic", interrupt)
    with pytest.raises(OSError):
        upgrades.upgrade(workspace, apply=True, backup=tmp_path / "backup.zip")
    assert (workspace / ".agent-dev/upgrade-journal.json").exists()
    with pytest.raises(DomainError):
        WorkflowRuntime(workspace)
    monkeypatch.setattr(upgrades, "atomic", original)
    assert upgrades.recover(workspace)["action"] == "rolled_back"
    assert upgrades.inputs(workspace / ".agent-dev") == before


def test_upgrade_finalizes_committed_journal(workspace):
    runtime = WorkflowRuntime(workspace)
    root = workspace / ".agent-dev"
    values = upgrades.inputs(root)
    journal = {
        "format_version": 1,
        "id": "fixture",
        "from_version": "0.2.0",
        "files": {
            name: {
                "old": base64.b64encode(value).decode(),
                "new": base64.b64encode(value).decode(),
                "old_digest": content_digest(value),
                "new_digest": content_digest(value),
            }
            for name, value in values.items()
        },
    }
    (root / "upgrade-journal.json").write_text(json.dumps(journal), encoding="utf-8")
    runtime.store.event("EXAMPLE", "WorkspaceUpgraded", {"upgrade_id": "fixture"})
    assert upgrades.recover(workspace)["action"] == "finalized"


def test_upgrade_active_or_newer_workspace_rejected(workspace, tmp_path):
    runtime = old_workspace(workspace)
    with runtime.safety.lease("upgrade-fixture"), pytest.raises(DomainError):
        upgrades.upgrade(workspace, apply=True, backup=tmp_path / "never.zip")
    assert not (tmp_path / "never.zip").exists()
    marker = workspace / ".agent-dev/workspace.json"
    value = json.loads(marker.read_text(encoding="utf-8"))
    value["engine_version"] = "9.0.0"
    marker.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(DomainError):
        upgrades.upgrade(workspace)


def approved_budget(root, **budget):
    managed = root / ".agent-dev/config/profile.json"
    value = json.loads(managed.read_text(encoding="utf-8"))
    value["execution_limits"] = dict(limits(), **budget)
    managed.write_text(json.dumps(value), encoding="utf-8")
    return approved(root)


class CountingRunner:
    def __init__(self):
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return PhaseResult(
            '```json\n{"handoff_status":"pass"}\n```',
            "fixture",
            {"input_tokens": 3, "output_tokens": 2},
        )


def test_attempt_budget_survives_resume_without_extra_provider_call(workspace):
    runtime = approved_budget(workspace, max_attempts=1)
    runner = CountingRunner()
    assert runtime.execute("T", "first", runner=runner)["reason"] == "EXECUTION_LIMIT_REACHED"
    assert runtime.execute("T", "second", runner=runner)["reason"] == "EXECUTION_LIMIT_REACHED"
    assert len(runner.requests) == 1
    result = TaskQueries(workspace).explain("T")
    assert result["execution"]["usage"]["attempts"] == 1
    assert any(row["code"] == "EXECUTION_LIMIT_REACHED" for row in result["reasons"])
    assert result["execution"]["usage"]["measurements"][0]["value"] == 3


def test_timeout_budget_and_usage_unknown_remain_explicit(workspace):
    runtime = approved_budget(workspace, phase_timeout_seconds=4, max_runner_seconds=3)
    runner = CountingRunner()
    assert runtime.execute("T", "first", runner=runner)["state"] == "verifying"
    assert all(1 <= request.timeout_seconds <= 3 for request in runner.requests)
    report = TaskQueries(workspace).usage("T")
    assert report["usage"]["measurements"][0]["value"] == 9
    assert report["usage"]["measurements"][2]["value"] is None
    assert report["cost"]["configured"] is False and report["cost"]["known_cost"] is None


def test_failed_quota_attempt_consumes_budget(workspace):
    runtime = approved_budget(workspace, max_attempts=1)

    class Quota:
        def execute(self, request):
            raise DomainError(ErrorCode.CAPACITY_EXHAUSTED, "fixture")

    assert runtime.execute("T", "quota", runner=Quota())["state"] == "waiting_for_capacity"
    assert (
        runtime.execute("T", "resume", runner=CountingRunner())["reason"]
        == "EXECUTION_LIMIT_REACHED"
    )


def test_usage_parsers_ignore_arbitrary_provider_data():
    assert codex_usage(
        "garbage\n"
        + json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 3, "output_tokens": 5, "secret": "never"},
            }
        )
    ) == {"input_tokens": 3, "output_tokens": 5}
    assert claude_result(json.dumps({"result": "output", "usage": {"input_tokens": 7}})) == (
        "output",
        {"input_tokens": 7},
    )
    assert safe_usage({"input_tokens": -1, "output_tokens": True}) == {}


def tasks(root, count=260):
    runtime = WorkflowRuntime(root)
    runtime.store.design(
        "EXAMPLE", "D", "## Scope\nquery\n## Acceptance\nfilter\n## Constraints\nreadonly"
    )
    runtime.store.approve("design", "D", 1, "fixture")
    runtime.store.create_plan(
        "EXAMPLE",
        "PLAN",
        "D",
        1,
        [
            {"id": f"T-{i:03d}", "title": "literal %_ safe" if i == 259 else f"Example {i}"}
            for i in range(count)
        ],
    )
    return runtime


def test_filtered_pagination_covers_all_tasks_and_literals(workspace):
    tasks(workspace)
    query = TaskQueries(workspace)
    first, second, third = [query.tasks(page=value) for value in [1, 2, 3]]
    assert first["total"] == 260 and first["pages"] == 3
    assert len(third["tasks"]) == 60 and not third["has_next"]
    assert len({row["id"] for report in [first, second, third] for row in report["tasks"]}) == 260
    assert query.tasks(search="%_")["filtered_total"] == 1
    assert query.tasks(search="' OR 1=1 --")["filtered_total"] == 0
    assert query.tasks(project="other")["filtered_total"] == 0
    assert query.tasks(state=first["tasks"][0]["state"])["filtered_total"] == 260


@pytest.mark.parametrize(
    "options", [{"page": 0}, {"limit": 201}, {"state": "invalid"}, {"search": "x" * 201}]
)
def test_invalid_query_filters_rejected(workspace, options):
    with pytest.raises(DomainError):
        TaskQueries(workspace).tasks(**options)


def test_cli_and_dashboard_filters_roundtrip(workspace, capsys):
    tasks(workspace, 4)
    main(
        [
            "task",
            "list",
            "--root",
            str(workspace),
            "--limit",
            "2",
            "--page",
            "2",
            "--search",
            "Example",
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["page"] == 2 and len(result["tasks"]) == 2
    server = make_server(workspace, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
        client.request("GET", "/api/tasks?search=Example&limit=2&page=2")
        response = client.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["page"] == 2
        client.request("GET", "/api/tasks?page=0")
        response = client.getresponse()
        assert response.status == 400
        response.read()
        client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_time_budget_counts_failed_attempts_before_resume(workspace):
    runtime = approved_budget(workspace, max_runner_seconds=2)

    class Failure:
        def execute(self, request):
            raise DomainError(ErrorCode.CAPACITY_EXHAUSTED, "fixture quota")

    runtime.execute("T", "failed", runner=Failure())
    attempt = runtime.store.db.rows("SELECT id FROM phase_attempts")[0]["id"]
    runtime.store.event(
        "T", "RunnerUsage", {"attempt_id": attempt, "runner_seconds": 2.1, "usage": {}}
    )
    runner = CountingRunner()
    assert runtime.execute("T", "resume-time", runner=runner)["reason"] == "EXECUTION_LIMIT_REACHED"
    assert not runner.requests


def test_profile_change_does_not_reset_approved_budget(workspace):
    runtime = approved_budget(workspace, max_attempts=1)
    runner = CountingRunner()
    runtime.execute("T", "first-budget", runner=runner)
    updated = json.loads((workspace / ".agent-dev/config/profile.json").read_text(encoding="utf-8"))
    updated["execution_limits"]["max_attempts"] = 100
    row = runtime.store.db.rows("SELECT version FROM projects WHERE id='EXAMPLE'")[0]
    runtime.store.revise_profile("EXAMPLE", updated, row["version"])
    assert (
        runtime.execute("T", "same-snapshot", runner=runner)["reason"] == "EXECUTION_LIMIT_REACHED"
    )
    assert len(runner.requests) == 1


def test_approved_reentry_limit_is_honored(workspace):
    runtime = approved_budget(workspace, max_reentries=2)

    class Findings:
        calls = 0
        qa_calls = 0

        def execute(self, request):
            self.calls += 1
            if request.phase_id == "qa":
                self.qa_calls += 1
                if self.qa_calls <= 2:
                    return PhaseResult(
                        "```json\n"
                        + json.dumps(
                            {
                                "handoff_status": "issue",
                                "issue_type": "fixture",
                                "root_cause": "same finding",
                                "owning_agent_id": "developer",
                                "reentry_step_id": "developer",
                            }
                        )
                        + "\n```"
                    )
            return PhaseResult('```json\n{"handoff_status":"pass"}\n```')

    runner = Findings()
    assert runtime.execute("T", "reentry", runner=runner)["state"] == "verifying"
    assert runner.qa_calls == 3 and runner.calls == 7


def test_parse_failure_has_safe_public_diagnostic(workspace):
    runtime = approved(workspace)

    class Malformed:
        def execute(self, request):
            return PhaseResult("not a control object")

    result = runtime.execute("T", "malformed", runner=Malformed())
    assert result["state"] == "blocked"
    assert result["reason"] == "Exactly one Harness Control block required."
    assert runtime.store.db.rows("SELECT kind FROM events WHERE kind='ExecutionBlocked'")


def test_provider_timeout_uses_approved_request(workspace, monkeypatch):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter
    from agent_dev.domain.contracts import PhaseRequest

    runtime = approved_budget(workspace, phase_timeout_seconds=7)
    adapter = CliRunnerAdapter("codex_cli", {}, workspace)
    seen = []
    monkeypatch.setattr(
        adapter.runner,
        "run",
        lambda *args: (
            seen.append(adapter.runner.timeout_seconds) or '```json\n{"handoff_status":"pass"}\n```'
        ),
    )
    task = runtime.store.task("T")
    request = PhaseRequest(
        task, "planner", "fixture", "digest", 1, frozenset({"code.read"}), "{}", timeout_seconds=7
    )
    adapter.execute(request)
    assert seen == [7]


def test_exhausted_budget_recovers_fenced_running_attempt(workspace, tmp_path):
    from agent_dev.domain.status import TaskStatus
    from agent_dev.domain.transitions import TransitionContext

    runtime = approved_budget(workspace, max_attempts=1)
    lease = runtime.safety.acquire("task:T", "crashed-fixture")
    task = runtime.store.task("T")
    runtime.store.transition(
        "T",
        TaskStatus.RUNNING,
        TransitionContext(
            design_approved=True,
            plan_approved=True,
            snapshot_created=True,
            dependencies_completed=True,
            preflight_passed=True,
            lease_acquired=True,
            expected_version=task.version,
        ),
        lease,
    )
    runtime.store.begin_attempt("T", "planner", "crashed-attempt", lease)
    runtime.safety.release(lease)
    runner = CountingRunner()
    result = runtime.execute("T", "recover-exhausted", runner=runner)
    assert result["reason"] == "EXECUTION_LIMIT_REACHED"
    assert not runner.requests
    assert runtime.store.db.rows("SELECT state FROM phase_attempts") == [{"state": "interrupted"}]
    from agent_dev.adapters.backup import create_bundle

    assert create_bundle(workspace, tmp_path / "recovered.zip")


def test_model_diagnostic_is_offline_and_preserves_global_config(tmp_path, monkeypatch):
    from agent_dev.adapters.diagnostics import codex_model_check

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text('model="unsupported-fixture"\n', encoding="utf-8")
    (tmp_path / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "supported-fixture"}]}), encoding="utf-8"
    )
    before = config.read_bytes()
    assert codex_model_check({})[0] == "warn"
    state, message = codex_model_check({"model": "supported-fixture"})
    assert state == "pass" and "실제 접근 권한은 확인하지 않았습니다" in message
    assert config.read_bytes() == before
    # No model anywhere: the CLI default runs, so there is nothing to warn about.
    config.unlink()
    assert codex_model_check({})[0] == "pass"
