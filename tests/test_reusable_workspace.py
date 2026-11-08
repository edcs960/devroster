import http.client
import json
import subprocess
import threading
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent_dev.adapters.backup import create_bundle, inspect_bundle, restore_bundle
from agent_dev.adapters.cli_runner import CliRunnerAdapter
from agent_dev.adapters.command_line import main
from agent_dev.adapters.diagnostics import diagnose
from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.adapters.workflow_runtime import WorkflowRuntime, project_root
from agent_dev.adapters.workspace import default_root, discover, initialize, portable_root
from agent_dev.domain.content import content_digest
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError
from agent_dev.ui.dashboard import make_server


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout.strip()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src/source.txt").write_text("baseline", encoding="utf-8")
    (root / ".gitignore").write_text("/.agent-dev/\n/output/\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "add", "--", "src/source.txt", ".gitignore")
    git(root, "commit", "-qm", "baseline")
    initialize(root, name="Example", project_id="EXAMPLE", paths=["src"])
    return root


def approved(root):
    runtime = WorkflowRuntime(root)
    profile = json.loads((root / ".agent-dev/config/profile.json").read_text(encoding="utf-8"))
    # These fixtures exercise the preserved legacy-linear three-phase contract.
    profile.pop("workflow",None)
    profile["role_context"] = {key:value for key,value in profile["role_context"].items() if key in {"planner","developer","qa"}}
    profile["runners"][0]["authenticated"] = True
    version = runtime.store.db.rows("SELECT version FROM projects WHERE id='EXAMPLE'")[0]["version"]
    runtime.store.revise_profile("EXAMPLE", profile, version)
    runtime.store.design(
        "EXAMPLE",
        "D",
        "## Scope\nChange source\n## Acceptance\nVerify diff\n## Constraints\nOnly src",
    )
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan(
        "EXAMPLE", "PLAN", "D", 1, [{"id": "T", "title": "Example task", "system_name": "Example"}]
    )
    runtime.store.approve("plan", "PLAN", 1, "user")
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    path = root / ".agent-dev/config/runtime.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["orchestrator_v2"]["execute"] = True
    path.write_text(json.dumps(config), encoding="utf-8")
    return runtime


class Runner:
    def execute(self, request):
        if request.phase_id == "developer":
            binding = json.loads(request.context)["repositories"]["source"]
            (Path(binding["worktree"]) / "src/source.txt").write_text("implemented", encoding="utf-8")
        return PhaseResult('```json\n{"handoff_status":"pass"}\n```')


def test_initialize_preserves_source_and_has_self_contained_roles(workspace):
    assert (workspace / "src/source.txt").read_text(encoding="utf-8") == "baseline"
    profile = json.loads((workspace / ".agent-dev/config/profile.json").read_text(encoding="utf-8"))
    assert profile["repositories"]["source"]["path"] == "."
    assert profile["workflow"]["id"] == "project-roles"
    assert profile["roles"]["domain"] == "generic"
    assert "fmea-expert" not in profile["roles"]["order"]
    assert set(profile["role_context"]) == {n["id"] for n in profile["workflow"]["document"]["nodes"] if n["kind"] == "role"}
    assert not profile["runners"][0]["authenticated"]
    # New workspaces run on the approval-based engine from the start (ADR 0003).
    assert WorkflowRuntime(workspace).flags()["execute"]
    before = (workspace / ".agent-dev/state.db").read_bytes()
    with pytest.raises(DomainError):
        initialize(workspace, name="Other")
    assert (workspace / ".agent-dev/state.db").read_bytes() == before


@pytest.mark.parametrize("path", ["../outside", ".git", ".agent-dev/file", "src\\file"])
def test_initialize_rejects_unsafe_allow_paths(tmp_path, path):
    root = tmp_path / "invalid"
    with pytest.raises(DomainError):
        initialize(root, paths=[path])
    assert not root.exists()


def test_two_projects_and_child_discovery_are_isolated(workspace, tmp_path):
    other = tmp_path / "other"
    initialize(other, name="Other")
    assert discover(workspace / "src") == workspace / ".agent-dev"
    assert discover(other / "nested") == other / ".agent-dev"
    first, second = WorkflowRuntime(workspace), WorkflowRuntime(other)
    assert project_root(workspace / ".agent-dev") == workspace
    assert first.store.db.path != second.store.db.path
    assert first.store.db.rows("SELECT id FROM projects") == [{"id": "EXAMPLE"}]
    assert second.store.db.rows("SELECT id FROM projects") != [{"id": "EXAMPLE"}]


def test_explicit_and_environment_contexts_are_distinct(workspace, tmp_path, monkeypatch):
    other = tmp_path / "other"
    initialize(other, name="Other")
    monkeypatch.chdir(other)
    monkeypatch.setenv("AGENT_DEV_CONFIG_ROOT", str(workspace / ".agent-dev"))
    assert default_root() == workspace / ".agent-dev"
    assert portable_root() == workspace / ".agent-dev"
    assert WorkflowRuntime(other).project_root == other


def test_readonly_queries_do_not_initialize_database(tmp_path):
    root = tmp_path / "uninitialized"
    root.mkdir()
    assert TaskQueries(root).tasks()["tasks"] == []
    report = diagnose(root)
    assert report["failed"] > 0
    assert not (root / ".agent-dev").exists()


def test_doctor_is_offline_and_discloses_unprobed_auth(workspace, monkeypatch):
    real = subprocess.run
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        assert command[0] == "git"
        return real(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", probe)
    report = diagnose(workspace)
    auth = next(row for row in report["checks"] if row["check"].startswith("auth:"))
    assert auth["status"] == "warn" and "확인하지 않았습니다" in auth["message"]
    assert calls


def test_doctor_explicit_auth_does_not_expose_provider_output(workspace, monkeypatch):
    real = subprocess.run

    def probe(command, **kwargs):
        if command[0] == "codex":
            return subprocess.CompletedProcess(command, 0, "secret-account-token", "")
        return real(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", probe)
    monkeypatch.setattr(
        "agent_dev.adapters.diagnostics.shutil.which", lambda name: "/fixture/" + name
    )
    report = diagnose(workspace, check_auth=True)
    assert "secret-account-token" not in json.dumps(report)
    assert any(
        row["check"].startswith("auth:") and row["status"] == "pass" for row in report["checks"]
    )


def test_runner_uses_own_registry_and_no_ambient_env_file(workspace, monkeypatch):
    def denied():
        raise AssertionError("An unrelated project's .env must not be loaded.")

    monkeypatch.setattr("agent_dev.runners.claude_cli.load_cli_env_files", denied)
    adapter = CliRunnerAdapter(
        "claude_cli", {}, workspace, {"db": str(workspace / ".agent-dev/state.db")}
    )
    assert (
        Path(adapter.runner.process_registry_path)
        == workspace / ".agent-dev/data/process_registry.json"
    )


def test_bundle_rejects_raw_secret_in_database(workspace, tmp_path):
    runtime = WorkflowRuntime(workspace)
    with runtime.store.db.transaction() as sql:
        sql.execute("UPDATE projects SET name=?", ("sk-" + "a" * 24,))
    with pytest.raises(DomainError):
        create_bundle(workspace, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()


def test_restore_preserves_existing_output(workspace, tmp_path):
    approved(workspace)
    path = workspace / "output/Example/T/notes.md"
    path.parent.mkdir(parents=True)
    path.write_text("Evidence", encoding="utf-8")
    archive = tmp_path / "backup.zip"
    create_bundle(workspace, archive)
    destination = tmp_path / "destination"
    existing = destination / "output/Example/T/notes.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("Keep my file", encoding="utf-8")
    with pytest.raises(DomainError):
        restore_bundle(archive, destination)
    assert existing.read_text(encoding="utf-8") == "Keep my file"
    assert not (destination / ".agent-dev").exists()


def test_invalid_bundle_has_a_structured_cli_error(tmp_path, capsys):
    archive = tmp_path / "invalid.zip"
    archive.write_bytes(b"not a zip")
    with pytest.raises(SystemExit):
        main(["backup", "restore", str(archive), str(tmp_path / "destination"), "--json"])
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "rejected"
    assert not (tmp_path / "destination").exists()


def test_profile_revision_cli_and_relative_worktree(workspace, capsys):
    path = workspace / ".agent-dev/config/profile.json"
    main(["project", "profile", "EXAMPLE", str(path), "--root", str(workspace), "--json"])
    assert json.loads(capsys.readouterr().out)["result"]["profile_version"] == 2
    runtime = approved(workspace)
    binding = runtime.store.snapshot("T")["resolved"]["repositories"]["source"]
    assert Path(binding["path"]) == workspace
    assert Path(binding["worktree"]).is_relative_to(workspace / ".agent-dev/worktrees")


def test_explain_reports_approval_and_required_gate(workspace):
    runtime = approved(workspace)
    query = TaskQueries(workspace)
    assert query.explain("T")["approvals"] == {"design": True, "plan": True, "snapshot": True}
    assert runtime.execute("T", "run", runner=Runner())["state"] == "verifying"
    assert any(row["code"] == "VERIFICATION_PENDING" for row in query.explain("T")["reasons"])
    assert runtime.execute("T", "verify", verify=True)["passed"]
    report = query.explain("T")
    assert len(report["attempts"]) == 3
    assert report["gates"][0]["passed"]
    assert report["timeline"][-1]["kind"] == "TaskStateChanged"
    assert (workspace / "src/source.txt").read_text(encoding="utf-8") == "baseline"
    with pytest.raises(DomainError):
        query.tasks(201)


def test_registered_evidence_matches_stored_bytes(workspace):
    runtime = approved(workspace)
    runtime.execute("T", "run", runner=Runner())
    runtime.execute("T", "verify", verify=True)
    with runtime.safety.lease("task:T") as lease:
        checkpoint = Path(runtime.artifacts.checkpoint("T", lease, reason="한글 상태 확인"))
    rows = runtime.store.db.rows("SELECT path,digest,kind FROM artifacts WHERE task_id='T'")
    assert {row["kind"] for row in rows} >= {
        "phase_prompt", "phase_output", "checkpoint", "verification"
    }
    for row in rows:
        payload = (workspace / "output" / row["path"]).read_bytes()
        assert content_digest(payload) == row["digest"], row["path"]
        assert b"\r\n" not in payload, row["path"]
        payload.decode("utf-8")
    assert "한글 상태 확인" in checkpoint.read_bytes().decode("utf-8")
    assert (runtime.artifacts.directory("T") / "latest-checkpoint.md").read_bytes() == (
        checkpoint.read_bytes()
    )


def test_bundle_roundtrip_evidence_and_revoked_rights(workspace, tmp_path):
    runtime = approved(workspace)
    runtime.execute("T", "run", runner=Runner())
    runtime.execute("T", "verify", verify=True)
    original_snapshot = runtime.store.snapshot("T")["digest"]
    (workspace / ".agent-dev/.env").write_text("CUSTOM_TOKEN=private-value\n", encoding="utf-8")
    config_path = workspace / ".agent-dev/config/runtime.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["api_key"] = "private-value"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    archive = tmp_path / "backup.zip"
    create_bundle(workspace, archive)
    assert inspect_bundle(archive)["valid"]
    with ZipFile(archive) as zipped:
        assert not any(path.endswith(".env") or "worktrees/" in path for path in zipped.namelist())
        assert b"private-value" not in zipped.read(".agent-dev/config/runtime.json")
    destination = tmp_path / "restored"
    report = restore_bundle(archive, destination)
    restored = WorkflowRuntime(destination)
    assert report["execution_disabled"] and not restored.flags()["execute"]
    assert not restored.store.approved("T")
    assert restored.store.task("T").state == "blocked"
    assert restored.store.snapshot("T")["digest"] == original_snapshot
    assert all(
        row["state"] in {"dead", "sent"}
        for row in restored.store.db.rows("SELECT state FROM notification_outbox")
    )
    profile = json.loads(
        restored.store.db.rows(
            "SELECT content_json FROM project_profile_versions ORDER BY version DESC LIMIT 1"
        )[0]["content_json"]
    )
    assert not profile["runners"][0]["authenticated"]
    assert restored.store.db.rows("PRAGMA foreign_key_check") == []
    artifact = restored.store.db.rows("SELECT path,digest FROM artifacts LIMIT 1")[0]
    assert (
        content_digest((destination / "output" / artifact["path"]).read_bytes())
        == artifact["digest"]
    )
    assert runtime.store.approved("T")
    with pytest.raises(DomainError):
        restored.validate_snapshot_paths("T")


def test_bundle_rejects_active_lease_and_existing_destination(workspace, tmp_path):
    runtime = WorkflowRuntime(workspace)
    archive = tmp_path / "backup.zip"
    with runtime.safety.lease("task:active"), pytest.raises(DomainError):
        create_bundle(workspace, archive)
    assert not archive.exists()
    create_bundle(workspace, archive)
    with pytest.raises(DomainError):
        create_bundle(workspace, archive)
    with pytest.raises(DomainError):
        restore_bundle(archive, workspace)


def test_bundle_rejects_tampering_and_path_escape(workspace, tmp_path):
    original = tmp_path / "original.zip"
    create_bundle(workspace, original)
    corrupted = tmp_path / "corrupted.zip"
    with ZipFile(original) as source, ZipFile(corrupted, "w") as target:
        for name in source.namelist():
            target.writestr(
                name, b"tampered" if name == ".agent-dev/config/runtime.json" else source.read(name)
            )
    with pytest.raises(DomainError):
        inspect_bundle(corrupted)
    hostile = tmp_path / "hostile.zip"
    payload = b"outside"
    with ZipFile(original) as source, ZipFile(hostile, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        manifest["entries"].append(
            {
                "path": "output/../../outside",
                "size": len(payload),
                "sha256": content_digest(payload),
            }
        )
        target.writestr("manifest.json", json.dumps(manifest))
        for name in source.namelist():
            if name != "manifest.json":
                target.writestr(name, source.read(name))
        target.writestr("output/../../outside", payload)
    with pytest.raises(DomainError):
        restore_bundle(hostile, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_bundle_rejects_secret_in_immutable_evidence(workspace, tmp_path, monkeypatch):
    runtime = approved(workspace)
    directory = workspace / "output/Example/T"
    directory.mkdir(parents=True)
    (directory / "leaked.md").write_text("private-evidence-secret", encoding="utf-8")
    monkeypatch.setenv("CUSTOM_TOKEN", "private-evidence-secret")
    with pytest.raises(DomainError):
        create_bundle(workspace, tmp_path / "backup.zip")
    assert runtime.store.task("T").state == "ready"


def test_dashboard_readonly_and_origin_boundary(workspace):
    runtime = approved(workspace)
    runtime.store.event(
        "T", "Example", {"evidence": '<img src=x onerror="alert(1)">', "api_key": "never-expose"}
    )
    server = make_server(workspace, 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request("GET", "/api/task?id=T")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Security-Policy")
        assert b"never-expose" not in response.read()
        connection.request("GET", "/api/tasks", headers={"Host": "attacker.example"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", "/api/tasks", headers={"Origin": "https://attacker.example"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("POST", "/api/task?id=T", body="cancel")
        response = connection.getresponse()
        assert response.status == 405
        response.read()
        connection.close()
        assert runtime.store.task("T").state == "ready"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize("logged_in", [False, True])
def test_doctor_reports_a_missing_login_once(workspace, monkeypatch, logged_in):
    """First-use review: a logged-out CLI showed both a login failure and a 'login off in the
    profile' warning that told the user to save the settings again."""
    real = subprocess.run

    def probe(command, **kwargs):
        if command[0] in {"codex", "claude"}:
            return subprocess.CompletedProcess(command, 0 if logged_in else 1, "{}", "")
        return real(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", probe)
    monkeypatch.setattr(
        "agent_dev.adapters.diagnostics.shutil.which", lambda name: "/fixture/" + name
    )
    rows = diagnose(workspace, check_auth=True)["checks"]
    policy = [row for row in rows if row["check"].startswith("runner_policy:")]
    if logged_in:
        assert all("로그인은 확인됐지만" in row["message"] for row in policy)
    else:
        assert any(r["check"].startswith("auth:") and r["status"] == "fail" for r in rows)
        assert policy == []
