"""Setup acceptance across CLI, HTTP, profiles, environments and actual runner boundaries."""

import contextlib
import copy
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from agent_dev.adapters.command_line import main
from agent_dev.adapters.environment import (
    MASK,
    environment_file,
    process_environment,
    read_environment,
)
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.studio_generation import StudioJobs
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.adapters.workspace import configuration_root, initialize
from agent_dev.application.studio import Studio
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.setup_server import make_server


def config(root):
    value = ProjectSetup(root).read()
    value["create_directories"] = True
    return value


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout.strip()


def commit(root):
    git(root, "init", "-q")
    git(root, "config", "user.name", "Setup Fixture")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")


def test_read_and_diagnose_do_not_create_project(tmp_path):
    root = tmp_path / "missing"
    service = ProjectSetup(root)
    assert service.read()["revision"] == 0
    assert not service.check()["ready"]
    assert not root.exists()


def test_initialize_secret_roundtrip_revision_and_events(tmp_path):
    root = tmp_path / "project"
    service = ProjectSetup(root)
    value = config(root)
    value["project_id"] = "custom-id"
    value["modules"] = [
        {
            "id": "frontend",
            "path": "frontend",
            "preset": "react",
            "allowed_paths": ["src", "package.json"],
            "base_ref": "HEAD",
        }
    ]
    value["verification"][0]["module"] = "frontend"
    value["environment"] = [{"key": "DATABASE_URL", "value": "postgresql://secret-value"}]
    result = service.save(value)
    assert (root / "frontend").is_dir()
    assert result["settings"]["project_id"] == "custom-id"
    assert result["settings"]["environment"] == [{"key": "DATABASE_URL", "value": MASK}]
    assert "secret-value" not in json.dumps(result)
    assert "secret-value" not in service.db_path.read_bytes().decode("latin1")
    loaded = ProjectSetup(root).read()
    assert loaded["profile_version"] == 1
    ProjectSetup(root).save(loaded)
    assert ProjectSetup(root).read()["revision"] == 2
    assert read_environment(root)["DATABASE_URL"] == "postgresql://secret-value"
    loaded = ProjectSetup(root).read()
    loaded["environment"] = []
    ProjectSetup(root).save(loaded)
    assert read_environment(root) == {}
    db = Database(service.db_path, read_only=True)
    assert len(db.rows("SELECT * FROM project_profile_versions")) == 3
    assert [r["kind"] for r in db.rows("SELECT kind FROM events ORDER BY version")] == [
        "ProjectRegistered",
        "ProfileRevised",
        "ProfileRevised",
    ]
    if os.name != "nt":
        assert environment_file(root).stat().st_mode & 0o777 == 0o600
    assert (root / ".agent-dev/.gitignore").read_text(encoding="utf-8") == "*\n"


def test_existing_init_and_studio_adoption(tmp_path):
    root = tmp_path / "initialized"
    initialize(root)
    service = ProjectSetup(root)
    before = json.loads((root / ".agent-dev/config/runtime.json").read_text(encoding="utf-8"))
    value = service.read()
    service.save(value)
    assert json.loads((root / ".agent-dev/config/runtime.json").read_text(encoding="utf-8")) == before
    assert service.read()["profile_version"] == 2
    other = tmp_path / "studio-first"
    store = StudioStore(other)
    service = ProjectSetup(other)
    service.save(config(other))
    assert store.path.is_file()
    assert configuration_root(other) == other.resolve() / ".agent-dev"


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowed_paths", ["."]),
        ("allowed_paths", ["../escape"]),
        ("allowed_paths", [".agent-dev"]),
        ("base_ref", "--all"),
        ("id", "../bad"),
        ("preset", "unknown"),
    ],
)
def test_unsafe_module_rejected_without_writes(tmp_path, field, value):
    root = tmp_path / "new"
    settings = config(root)
    settings["modules"][0][field] = value
    with pytest.raises(DomainError):
        ProjectSetup(root).save(settings)
    assert not root.exists()


@pytest.mark.parametrize(
    "key",
    [
        "PATH",
        "HOME",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "LD_PRELOAD",
        "DYLD_LIBRARY_PATH",
        "GIT_CONFIG_COUNT",
        "bad-name",
    ],
)
def test_environment_injection_keys_rejected(tmp_path, key):
    settings = config(tmp_path / "new")
    settings["environment"] = [{"key": key, "value": "injection"}]
    with pytest.raises(DomainError):
        ProjectSetup(tmp_path / "new").save(settings)


def test_stale_setting_and_external_profile_are_fenced(tmp_path):
    service = ProjectSetup(tmp_path)
    service.save(config(tmp_path))
    first, second = service.read(), service.read()
    service.save(first)
    with pytest.raises(DomainError) as error:
        service.save(second)
    assert error.value.code == ErrorCode.STALE_VERSION
    latest = service.read()
    store = WorkflowRepository(Database(service.db_path))
    row = store.db.rows("SELECT version FROM projects WHERE id=?", (latest["project_id"],))[0]
    profile = service.current_profile()[2]
    store.revise_profile(latest["project_id"], profile, row["version"])
    with pytest.raises(DomainError) as error:
        service.save(latest)
    assert error.value.code == ErrorCode.STALE_VERSION


def test_monorepo_binding_prefix_and_separate_repositories(tmp_path):
    root = tmp_path / "mono"
    for module in ["frontend", "backend"]:
        (root / module / "src").mkdir(parents=True)
        (root / module / "src/base.txt").write_text("baseline", encoding="utf-8")
    commit(root)
    settings = config(root)
    settings["modules"] = [
        {
            "id": name,
            "path": name,
            "preset": "generic",
            "allowed_paths": ["src"],
            "base_ref": "HEAD",
        }
        for name in ["frontend", "backend"]
    ]
    settings["verification"] = [
        {
            "name": name + "_unit",
            "module": name,
            "command": [sys.executable, "-c", 'print("OK")'],
            "cwd": ".",
            "timeout_seconds": 20,
            "required": True,
        }
        for name in ["frontend", "backend"]
    ]
    _, profile, _ = ProjectSetup(root).prepare(settings)
    assert len(profile["repositories"]) == 1
    assert profile["repositories"]["frontend"]["allowed_paths"] == ["frontend/src", "backend/src"]
    assert [g["cwd"] for g in profile["verification"]["commands"]] == ["frontend", "backend"]
    separate = tmp_path / "separate"
    (separate / "src").mkdir(parents=True)
    (separate / "src/base.txt").write_text("base", encoding="utf-8")
    commit(separate)
    settings["modules"][1]["path"] = str(separate)
    _, profile, _ = ProjectSetup(root).prepare(settings)
    assert len(profile["repositories"]) == 2
    assert profile["verification"]["commands"][1]["cwd"] == "."


def test_process_environment_actual_child_without_global_changes(tmp_path):
    settings = config(tmp_path)
    settings["environment"] = [{"key": "APP_SETUP_FIXTURE", "value": "child-value"}]
    ProjectSetup(tmp_path).save(settings)
    result = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['APP_SETUP_FIXTURE'])"],
        env=process_environment(tmp_path, ["APP_SETUP_FIXTURE"]),
        check=True,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    assert result.stdout.strip() == "child-value"
    assert "APP_SETUP_FIXTURE" not in os.environ
    with pytest.raises(DomainError):
        process_environment(tmp_path, ["MISSING_ENV"])


def test_cli_import_show_and_default_current_folder(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    settings = config(root)
    settings["ai"]["runners"]["codex_cli"]["model"] = "my-local-model"
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    main(["setup", "--root", str(root), "--from", str(path), "--json"])
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    monkeypatch.chdir(root)
    main(["setup", "--show", "--json"])
    output = json.loads(capsys.readouterr().out)["result"]
    assert output["ai"]["runners"]["codex_cli"]["model"] == "my-local-model"
    assert output["root"] == str(root.resolve())


@contextlib.contextmanager
def server(root):
    instance = make_server(root, 0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(instance.server_port)
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def request(base, path, payload=None, token="", headers=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=dict(
            {"Content-Type": "application/json", "X-Setup-Token": token}, **(headers or {})
        ),
    )
    try:
        response = urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read()


def test_web_save_masking_csrf_conflict_and_directory_only_browser(tmp_path):
    (tmp_path / "visible").mkdir()
    (tmp_path / "private.txt").write_text("private content", encoding="utf-8")
    with server(tmp_path) as base:
        status, data = request(base, "/api/setup/bootstrap")
        bootstrap = json.loads(data)
        assert status == 200
        value = bootstrap["settings"]
        value["environment"] = [{"key": "APP_TOKEN", "value": "web-private-value"}]
        payload = {"root": str(tmp_path), "settings": value}
        assert request(base, "/api/setup/save", payload)[0] == 403
        assert (
            request(
                base,
                "/api/setup/save",
                payload,
                bootstrap["token"],
                {"Origin": "https://evil.test"},
            )[0]
            == 403
        )
        status, data = request(base, "/api/setup/save", payload, bootstrap["token"])
        assert status == 200 and b"web-private-value" not in data
        assert request(base, "/api/setup/save", payload, bootstrap["token"])[0] == 409
        status, data = request(base, "/api/setup/folders")
        assert status == 200 and b"private.txt" not in data and b"visible" in data
        assert request(base, "/setup")[0] == 200
        assert request(base, "/setup.js")[0] == 200


def test_saved_defaults_reach_studio_job_config(tmp_path):
    from test_studio import BRIEF, specifications

    settings = config(tmp_path)
    settings["ai"]["provider"] = "claude_cli"
    settings["ai"]["runners"]["claude_cli"] = {
        "enabled": True,
        "model": "configured-model",
        "command": "configured-claude",
    }
    ProjectSetup(tmp_path).save(settings)
    studio = Studio(StudioStore(configuration_root(tmp_path)), None)
    item = studio.create("Fixture", BRIEF)
    called = []

    def generation(kind, data, provider, model, workspace, grant):
        called.append((provider, model, grant["runner_config"]["command"]))
        return specifications()

    jobs = StudioJobs(studio, generation, config_root=configuration_root(tmp_path))
    try:
        jobs.start(item["project"]["id"], item["project"]["version"], "specs")
        for _ in range(100):
            if called:
                break
            time.sleep(0.02)
        # The first call is enough: staged specs make more calls with the same configuration.
        assert called[0] == ("claude_cli", "configured-model", "configured-claude")
    finally:
        jobs.close()


def test_approved_workflow_uses_module_cwd_and_env_and_keeps_snapshot(tmp_path):
    root = tmp_path / "project"
    (root / "frontend/src").mkdir(parents=True)
    (root / "frontend/src/base.txt").write_text("base", encoding="utf-8")
    commit(root)
    settings = config(root)
    settings["modules"] = [
        {
            "id": "frontend",
            "path": "frontend",
            "preset": "generic",
            "allowed_paths": ["src"],
            "base_ref": "HEAD",
        }
    ]
    settings["roles"]["session_mode"] = False  # one runner call per role in this check
    settings["environment"] = [{"key": "APP_SETUP_PROBE", "value": "approved-value"}]
    settings["verification"] = [
        {
            "name": "unit",
            "module": "frontend",
            "command": [
                sys.executable,
                "-c",
                "import os,pathlib; assert pathlib.Path('src/base.txt').exists(); assert os.environ['APP_SETUP_PROBE']=='approved-value'; print('verified')",
            ],
            "cwd": ".",
            "required": True,
            "timeout_seconds": 20,
        }
    ]
    # The command must reference the environment name, rather than embedding its private value.
    settings["verification"][0]["command"][-1] = (
        "import os,pathlib; assert pathlib.Path('src/base.txt').exists(); assert os.environ['APP_SETUP_PROBE']; print('verified')"
    )
    service = ProjectSetup(root)
    service.save(settings)
    runtime_path = root / ".agent-dev/config/runtime.json"
    runtime_config = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime_config["orchestrator_v2"]["execute"] = True
    runtime_path.write_text(json.dumps(runtime_config), encoding="utf-8")
    runtime = WorkflowRuntime(root)
    project = service.read()["project_id"]
    runtime.store.design(
        project,
        "D",
        "## Scope\nSetup fixture\n## Acceptance\nEnvironment available\n## Constraints\nApproved only",
    )
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan(project, "P", "D", 1, [{"id": "T", "title": "Fixture"}])
    runtime.store.approve("plan", "P", 1, "user")
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    before = copy.deepcopy(runtime.store.snapshot("T"))
    newer = service.read()
    newer["ai"]["runners"]["codex_cli"]["model"] = "later-model"
    service.save(newer)
    assert runtime.store.snapshot("T") == before

    class Runner:
        def execute(self, request):
            return PhaseResult('```json\n{"handoff_status":"pass"}\n```')

    assert runtime.execute("T", "run", runner=Runner())["state"] == "verifying"
    assert runtime.execute("T", "verify", verify=True)["passed"]


def test_copy_settings_without_credentials_rebases_internal_paths(tmp_path, capsys):
    original = tmp_path / "origin"
    settings = config(original)
    settings["modules"][0]["path"] = str(original)
    settings["environment"] = [{"key": "APP_TOKEN", "value": "copy-private-value"}]
    ProjectSetup(original).save(settings)
    export = tmp_path / "export.json"
    export.write_text(json.dumps(ProjectSetup(original).read()), encoding="utf-8")
    target = tmp_path / "new-project"
    main(["setup", "--root", str(target), "--from", str(export), "--copy", "--json"])
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["settings"]["project_id"] == "new-project"
    assert result["settings"]["modules"][0]["path"] == "."
    assert read_environment(target) == {"APP_TOKEN": ""}
    assert result["warnings"]
    assert "copy-private-value" not in target.joinpath(".agent-dev/state.db").read_bytes().decode(
        "latin1"
    )
    settings = ProjectSetup(target).read()
    settings["environment"] = [
        {"key": "APP_TOKEN", "value": "target-private-value"},
        {"key": "TARGET_ONLY_TOKEN", "value": "target-extra-value"},
    ]
    ProjectSetup(target).save(settings)
    main(["setup", "--root", str(target), "--from", str(export), "--copy", "--json"])
    capsys.readouterr()
    assert read_environment(target) == {
        "APP_TOKEN": "target-private-value",
        "TARGET_ONLY_TOKEN": "target-extra-value",
    }




def test_legacy_project_folder_resolves_to_engine_settings(tmp_path, monkeypatch):
    from agent_dev.core import paths

    root = tmp_path / "legacy-project"
    engine = root / "devroster-engine"
    (engine / "config").mkdir(parents=True)
    (engine / "config/runtime.json").write_text(
        json.dumps({"orchestrator_v2": {"read": True, "execute": True, "publish": False}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "AGENT_DEV_ROOT", engine)
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    monkeypatch.setattr("agent_dev.runners.common.load_cli_env_files", lambda: None)
    service = ProjectSetup(root)
    assert service.legacy and service.config == engine.resolve()
    service.save(service.read())
    assert not (root / ".agent-dev/workspace.json").exists()
    assert not (root / ".agent-dev/config").exists()
    runtime = json.loads((engine / "config/runtime.json").read_text(encoding="utf-8"))
    assert runtime["orchestrator_v2"] == {"read": True, "execute": True, "publish": False}


def test_execution_from_the_project_folder_reads_the_engine_flags(tmp_path, monkeypatch):
    """The project folder and the engine folder resolve the same v2 flags and DB."""
    from agent_dev.adapters.workflow_runtime import WorkflowRuntime
    from agent_dev.core import paths

    root = tmp_path / "legacy-project"
    engine = root / "devroster-engine"
    (engine / "config").mkdir(parents=True)
    (engine / "config/runtime.json").write_text(
        json.dumps({"orchestrator_v2": {"read": True, "execute": True}}), encoding="utf-8"
    )
    monkeypatch.setattr(paths, "AGENT_DEV_ROOT", engine)
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    ProjectSetup(root).save(ProjectSetup(root).read())
    from_project, from_engine = WorkflowRuntime(root), WorkflowRuntime(engine)
    assert from_project.flags()["execute"] is True
    assert from_project.root == from_engine.root == engine.resolve()
    assert from_project.store.db.path == from_engine.store.db.path


def test_environment_write_failure_rolls_back_settings_and_profile(tmp_path, monkeypatch):
    from agent_dev.adapters import project_setup

    service = ProjectSetup(tmp_path)
    service.save(config(tmp_path))
    value = service.read()
    value["environment"] = [{"key": "APP_TOKEN", "value": "new-private-value"}]
    original = project_setup.atomic_json
    calls = []

    def failing(path, data):
        if path == environment_file(tmp_path) and not calls:
            calls.append(True)
            raise OSError("fixture failure")
        return original(path, data)

    monkeypatch.setattr(project_setup, "atomic_json", failing)
    with pytest.raises(OSError):
        service.save(value)
    assert service.read()["revision"] == 1
    assert service.current_profile()[1] == 1
    assert read_environment(tmp_path) == {}


def test_interactive_wizard_uses_shared_service(tmp_path, monkeypatch):
    from agent_dev.adapters.setup_cli import wizard

    initialize(tmp_path)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr(
        "agent_dev.adapters.project_setup.auth_probe", lambda provider, command: True
    )
    result = wizard(tmp_path)
    assert result["settings"]["revision"] == 1
    assert ProjectSetup(tmp_path).current_profile()[2]["runners"][0]["authenticated"] is True


def test_only_approved_sibling_worktrees_can_be_added(tmp_path):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter

    first, second = tmp_path / "task/frontend", tmp_path / "task/backend"
    first.mkdir(parents=True)
    second.mkdir()
    adapter = CliRunnerAdapter("codex_cli", {"approved_add_dirs": [str(second)]}, first)
    assert adapter.runner.add_dirs == [second]
    with pytest.raises(DomainError):
        CliRunnerAdapter("codex_cli", {"approved_add_dirs": [str(tmp_path / "outside")]}, first)


def test_korean_workspace_name_roundtrip(tmp_path):
    root = tmp_path / "신규프로젝트"
    service = ProjectSetup(root)
    settings = config(root)
    settings["name"] = "기획과 개발"
    service.save(settings)
    assert ProjectSetup(root).read()["project_id"] == "신규프로젝트"
    assert ProjectSetup(root).read()["name"] == "기획과 개발"


def test_backup_includes_settings_but_excludes_private_environment(tmp_path):
    from zipfile import ZipFile

    from agent_dev.adapters.backup import create_bundle, restore_bundle

    root = tmp_path / "original"
    service = ProjectSetup(root)
    settings = config(root)
    settings["environment"] = [{"key": "APP_TOKEN", "value": "backup-private-value"}]
    service.save(settings)
    backup = tmp_path / "backup.zip"
    create_bundle(root, backup)
    with ZipFile(backup) as archive:
        assert ".agent-dev/environment.json" not in archive.namelist()
        assert all(b"backup-private-value" not in archive.read(name) for name in archive.namelist())
    restored = tmp_path / "restored"
    restore_bundle(backup, restored)
    loaded = ProjectSetup(restored).read()
    assert loaded["revision"] == 1
    assert loaded["environment"] == [{"key": "APP_TOKEN", "value": ""}]


def test_new_project_runs_on_the_approval_engine_and_closes_locally(tmp_path):
    """ADR 0003: a new project can run, publish and complete without extra switches."""
    project = tmp_path / "fresh"
    project.mkdir()
    git(project, "init", "-q")
    (project / "README.md").write_text("fresh", encoding="utf-8")
    git(project, "add", ".")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    service = ProjectSetup(project)
    value = service.read()
    assert value["test_paths"] == ["tests", "src/test", "*.test.*", "*.spec.*"]
    assert value["document_paths"] == ["docs", "README.md"]
    assert value["completion_mode"] == "local"
    service.save(value)
    runtime = json.loads((service.config / "config/runtime.json").read_text(encoding="utf-8"))
    flags = runtime["orchestrator_v2"]
    assert flags["execute"] is True and flags["publish"] is True
    profile = service.current_profile()[2]
    assert profile["completion"] == {"mode": "local"}
    assert profile["document_paths"] == ["docs", "README.md"]
    saved = service.read()
    saved["completion_mode"] = "github"
    service.save(saved)
    assert service.current_profile()[2]["completion"] == {"mode": "github"}
    broken = service.read()
    broken["completion_mode"] = "ftp"
    with pytest.raises(DomainError):
        service.save(broken)


def test_preset_gates_leave_out_missing_npm_scripts(tmp_path):
    """The React defaults added `npm run lint` to a project without lint."""
    web, api = tmp_path / "web", tmp_path / "api"
    web.mkdir()
    api.mkdir()
    (web / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}), encoding="utf-8")
    result = ProjectSetup(tmp_path).preset_gates(
        [
            {"id": "frontend", "preset": "react", "path": "web"},
            {"id": "backend", "preset": "spring-boot-gradle", "path": "api"},
        ]
    )
    assert [g["name"] for g in result["gates"]] == [
        "frontend_unit",
        "frontend_build",
        "backend_build",
    ]
    assert [s["name"] for s in result["skipped"]] == ["frontend_lint"]
    # Without a readable package.json nothing is left out.
    (web / "package.json").unlink()
    names = [g["name"] for g in ProjectSetup(tmp_path).preset_gates(
        [{"id": "frontend", "preset": "react", "path": "web"}]
    )["gates"]]
    assert names == ["frontend_lint", "frontend_unit", "frontend_build"]


def test_new_project_starts_from_the_preset_its_build_files_suggest(tmp_path):
    """First use: a React folder opened with the whitespace check as its only gate."""
    root = tmp_path / "web"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}), encoding="utf-8"
    )
    value = ProjectSetup(root).read()
    (module,) = value["modules"]
    assert module["preset"] == "react"
    gates = [gate["name"] for gate in value["verification"]]
    assert gates == ["unit", "build"]  # no lint script, so no `npm run lint` gate
    # Several build files leave the choice to the user.
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert ProjectSetup(root).read()["modules"][0]["preset"] == "generic"


def test_first_project_makes_its_ai_tools_the_shared_ones(tmp_path, monkeypatch):
    """First use: with no shared AI yet a new project kept its own AI copy, while the setup
    step and the AI tools menu both said login and models live in the menu."""
    from agent_dev.adapters.global_ai import GlobalAi

    monkeypatch.setattr(
        "agent_dev.adapters.project_setup.auth_probe", lambda *_: {"state": "unknown"}
    )
    first = tmp_path / "first"
    first.mkdir()
    with server(first) as base:
        bootstrap = json.loads(request(base, "/api/setup/bootstrap")[1])
        assert bootstrap["settings"]["ai_tools"] is None
        payload = {"root": str(first), "settings": bootstrap["settings"]}
        status, data = request(base, "/api/setup/save", payload, bootstrap["token"])
    saved = json.loads(data)
    assert status == 200
    assert saved["shared_ai"]["creates_shared"] is True
    assert saved["settings"]["ai_tools"] == {"selected": saved["shared_ai"]["selected"]}
    assert GlobalAi().read()["revision"] == 1
    # The next project starts on the shared tools.
    second = tmp_path / "second"
    second.mkdir()
    assert ProjectSetup(second).read()["ai_tools"] == {"selected": saved["shared_ai"]["selected"]}
