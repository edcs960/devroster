"""Settings that arrive with a project folder never run before the user trusts them (audit S-01, S-02)."""

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from agent_dev.adapters import ai_hooks, workspace_trust
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.errors import DomainError, ErrorCode


def write_hooks(root, value):
    path = root / ".ai/hooks/hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def made_here(tmp_path, name="mine"):
    root = tmp_path / name
    root.mkdir()
    initialize(root)
    return root


def arrived_with_repository(tmp_path, source):
    """The attacker's repository: a whole .agent-dev copied into a new clone."""
    clone = tmp_path / "clone"
    shutil.copytree(source, clone)
    return clone


def test_a_workspace_made_here_is_trusted(tmp_path):
    root = made_here(tmp_path)
    assert workspace_trust.status(root) | {"needs_trust": True, "trusted": True} == workspace_trust.status(root)


def test_a_cloned_agent_dev_is_untrusted_until_reviewed(tmp_path):
    clone = arrived_with_repository(tmp_path, made_here(tmp_path))
    state = workspace_trust.status(clone)
    assert state["needs_trust"] and not state["trusted"]
    with pytest.raises(DomainError) as error:
        workspace_trust.require(clone, "작업 실행")
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED
    assert workspace_trust.trust(clone)["trusted"]
    workspace_trust.require(clone, "작업 실행")


def test_a_forged_hook_approval_in_the_repository_is_ignored(tmp_path):
    """S-01: the repository ships hooks and an approval file that matches them."""
    source = made_here(tmp_path)
    write_hooks(source, {"studio_confirmed": [{"run": [sys.executable, "-c", "print(1)"]}]})
    clone = arrived_with_repository(tmp_path, source)
    config = ai_hooks._config(clone)
    digest = ai_hooks.read_hooks(clone)["digest"]
    (config / ai_hooks.APPROVAL_FILE).write_text(json.dumps({"digest": digest}), encoding="utf-8")
    assert ai_hooks.overview(clone, config)["approved"] is False
    with pytest.raises(DomainError):
        ai_hooks.snapshot_value(clone, config)
    assert ai_hooks.studio_event(clone, {"project_id": "x"}) is None
    with pytest.raises(DomainError):
        ai_hooks.approve(config, clone, digest)  # Trust the settings first.
    workspace_trust.trust(clone)
    assert ai_hooks.overview(clone, config)["approved"] is False  # Hooks still need their own approval.
    ai_hooks.approve(config, clone, digest)
    assert ai_hooks.overview(clone, config)["approved"] is True
    # The forged file in the project was never touched or used.
    assert json.loads((config / ai_hooks.APPROVAL_FILE).read_text(encoding="utf-8")) == {"digest": digest}


def test_hook_approval_lives_outside_the_project(tmp_path):
    root = made_here(tmp_path)
    write_hooks(root, {"after_edit": [{"run": ["x"]}]})
    config = ai_hooks._config(root)
    ai_hooks.approve(config, root, ai_hooks.read_hooks(root)["digest"])
    assert not (config / ai_hooks.APPROVAL_FILE).exists()
    assert ai_hooks.overview(root, config)["approved"] is True


def test_another_database_at_a_trusted_path_is_not_trusted(tmp_path):
    root = made_here(tmp_path)
    foreign = arrived_with_repository(tmp_path, made_here(tmp_path, "other"))
    database = root / ".agent-dev/state.db"
    before = workspace_trust._identity(database)
    for suffix in ("", "-wal", "-shm"):
        (root / f".agent-dev/state.db{suffix}").unlink(missing_ok=True)
    time.sleep(0.05)  # Linux stamps birth times from a clock that ticks every 1-10 ms.
    shutil.copy2(foreign / ".agent-dev/state.db", database)
    replaced = workspace_trust._identity(database)
    if replaced == before and len(replaced) == 2:
        pytest.skip("이 파일 시스템은 inode 번호를 다시 쓰고 파일 생성 시각을 알려 주지 않습니다.")
    assert workspace_trust.status(root)["trusted"] is False


def reused_inode(monkeypatch, born):
    """A filesystem that gives the next file the same inode number and reports birth times."""
    monkeypatch.setattr(workspace_trust, "_born", lambda path: (Path(path).stat().st_ino, born["at"]))


def test_a_reused_inode_number_is_told_apart_by_birth_time(tmp_path, monkeypatch):
    """ext4 gives a freed inode number to the next new file (seen on Linux CI for 0.4.7)."""
    born = {"at": 1_000}
    reused_inode(monkeypatch, born)
    root = made_here(tmp_path)
    write_hooks(root, {"after_edit": [{"run": ["x"]}]})
    config = ai_hooks._config(root)
    ai_hooks.approve(config, root, ai_hooks.read_hooks(root)["digest"])
    assert workspace_trust.status(root)["trusted"] is True
    born["at"] = 2_000  # Same path, same device and inode, another file.
    assert workspace_trust.status(root)["trusted"] is False
    assert workspace_trust.hook_approval(config) == ""


def test_decisions_from_the_previous_format_are_kept_once(tmp_path, monkeypatch):
    root = made_here(tmp_path)
    database = root / ".agent-dev/state.db"
    info = database.stat()
    trust_file = workspace_trust._path()
    value = json.loads(trust_file.read_text(encoding="utf-8"))
    key = next(iter(value["databases"]))
    value["version"] = 1
    value["databases"][key]["file"] = [info.st_dev, info.st_ino]
    value["hooks"][key] = {"origin": "hooks", "file": [info.st_dev, info.st_ino], "digest": "d"}
    trust_file.write_text(json.dumps(value), encoding="utf-8")
    born = {"at": 1_000}
    reused_inode(monkeypatch, born)

    assert workspace_trust.status(root)["trusted"] is True  # Read before anything is stored.
    workspace_trust.ensure_seeded()
    stored = json.loads(trust_file.read_text(encoding="utf-8"))
    assert stored["version"] == workspace_trust.VERSION
    assert stored["databases"][key]["file"] == [info.st_dev, info.st_ino, 1_000]
    assert stored["hooks"][key]["file"] == [info.st_dev, info.st_ino, 1_000]
    assert workspace_trust.status(root)["trusted"] is True
    assert workspace_trust.hook_approval(root) == "d"

    born["at"] = 2_000
    assert workspace_trust.status(root)["trusted"] is False


def test_an_old_style_identity_no_longer_matches_once_stored(tmp_path, monkeypatch):
    root = made_here(tmp_path)
    info = (root / ".agent-dev/state.db").stat()
    trust_file = workspace_trust._path()
    value = json.loads(trust_file.read_text(encoding="utf-8"))
    assert value["version"] == workspace_trust.VERSION
    key = next(iter(value["databases"]))
    value["databases"][key]["file"] = [info.st_dev, info.st_ino]
    trust_file.write_text(json.dumps(value), encoding="utf-8")
    assert workspace_trust.status(root)["trusted"] == (len(workspace_trust._identity(key)) == 2)
    reused_inode(monkeypatch, {"at": 1_000})
    assert workspace_trust.status(root)["trusted"] is False


@pytest.mark.skipif(sys.platform != "linux", reason="statx는 Linux에만 있습니다.")
def test_linux_reports_the_inode_it_measured(tmp_path):
    path = tmp_path / "file"
    path.write_text("x", encoding="utf-8")
    born = workspace_trust._born(path)
    if born is None:
        pytest.skip("이 파일 시스템은 파일 생성 시각을 알려 주지 않습니다.")
    assert born[0] == path.stat().st_ino
    assert abs(born[1] / 1e9 - path.stat().st_mtime) < 60
    assert workspace_trust._identity(path) == [path.stat().st_dev, born[0], born[1]]
    assert workspace_trust._born(tmp_path / "missing") is None


def test_upgrading_trusts_projects_the_app_already_knew(tmp_path):
    from agent_dev.adapters.workspace_registry import register_root

    known = arrived_with_repository(tmp_path, made_here(tmp_path))
    register_root(known)
    (workspace_trust._path()).unlink()  # As before this version: no trust file yet.
    stranger = tmp_path / "stranger"
    shutil.copytree(known, stranger)
    assert workspace_trust.status(known)["trusted"] is True
    assert workspace_trust.status(stranger)["trusted"] is False


def test_untrusted_settings_do_not_run_login_probes_or_studio(tmp_path, monkeypatch):
    """S-02: opening the settings page must not run a runner command from the clone."""
    from agent_dev.adapters import project_setup
    from agent_dev.adapters.studio_generation import StudioJobs
    from agent_dev.ui.setup_server import cli_login_request

    clone = arrived_with_repository(tmp_path, made_here(tmp_path))
    calls = []
    monkeypatch.setattr(project_setup, "auth_probe", lambda provider, command: calls.append(command) or True)
    result = cli_login_request(ProjectSetup(clone), "cli-auth", {})
    assert result["trust_required"] is True and calls == []
    with pytest.raises(DomainError) as error:
        cli_login_request(ProjectSetup(clone), "cli-login", {"provider": "codex_cli"})
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED

    from importlib.resources import files

    from agent_dev.adapters.studio_store import StudioStore
    from agent_dev.application.studio import Studio

    studio = Studio(StudioStore(clone), files("agent_dev.ui").joinpath("assets"))
    item = studio.create("기획", "# 기획\n\n내용")
    jobs = StudioJobs(studio, config_root=clone)
    try:
        with pytest.raises(DomainError) as error:
            jobs.start(item["project"]["id"], item["project"]["version"], "specs")
        assert error.value.code == ErrorCode.APPROVAL_REQUIRED
    finally:
        jobs.close()
    check = ProjectSetup(clone).check(check_auth=True)
    assert any(row.get("check") == "workspace_trust" for row in check["checks"]) and calls == []


def test_untrusted_runtime_refuses_to_approve_or_run_tasks(tmp_path):
    from agent_dev.adapters.workflow_runtime import WorkflowRuntime

    clone = arrived_with_repository(tmp_path, made_here(tmp_path))
    runtime = WorkflowRuntime(clone)
    for call in (lambda: runtime.resolve_snapshot("T"), lambda: runtime.execute("T", "r1"), lambda: runtime.execute("T", "r2", verify=True)):
        with pytest.raises(DomainError) as error:
            call()
        assert error.value.code == ErrorCode.APPROVAL_REQUIRED


def test_review_lists_what_would_run(tmp_path):
    source = made_here(tmp_path)
    service = ProjectSetup(source)
    settings = service.read()
    settings["verification"] = [{"name": "unit", "module": settings["modules"][0]["id"], "command": ["make", "steal"], "timeout_seconds": 60}] if settings.get("modules") else []
    write_hooks(source, {"after_edit": [{"run": ["npm", "run", "lint"]}]})
    clone = arrived_with_repository(tmp_path, source)
    items = workspace_trust.review(clone)["items"]
    commands = [" ".join(item["command"]) for item in items]
    assert "npm run lint" in commands
    assert any(item["kind"].startswith("훅") for item in items)
    workspace_trust.trust(clone)
    assert workspace_trust.review(clone)["items"] == []


def test_untrusted_project_mcp_server_is_not_started(tmp_path):
    from agent_dev.adapters.integrations import Integrations

    clone = arrived_with_repository(tmp_path, made_here(tmp_path))
    started = []
    tools = Integrations(clone, client_factory=lambda *args, **kwargs: started.append(args))
    row = {"transport": "stdio", "command": [sys.executable, "-c", "pass"], "environment_keys": [], "credential_ref": "", "id": "c", "name": "c", "endpoint": ""}
    with pytest.raises(DomainError) as error:
        tools._client(row)
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED and "설정 신뢰" in str(error.value) and started == []
    workspace_trust.trust(clone)
    # Trusting the folder is not the same as confirming this server's launch command.
    with pytest.raises(DomainError) as error:
        tools._client(row)
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED and "실행 확인" in str(error.value) and started == []
    tools.approve_launch(row, tools.launch(row)["digest"])
    tools._client(row)
    assert len(started) == 1


def test_seeding_happens_before_a_new_folder_is_registered(tmp_path):
    """First launch after an upgrade with --root <fresh clone>: the clone is not grandfathered."""
    from agent_dev.adapters.workspace_registry import register_root

    known = arrived_with_repository(tmp_path, made_here(tmp_path))
    register_root(known)
    workspace_trust._path().unlink()
    workspace_trust.ensure_seeded()  # What the entry points do first.
    newcomer = tmp_path / "newcomer"
    shutil.copytree(known, newcomer)
    register_root(newcomer)  # Then the command registers the folder it was given.
    assert workspace_trust.status(known)["trusted"] is True
    assert workspace_trust.status(newcomer)["trusted"] is False
