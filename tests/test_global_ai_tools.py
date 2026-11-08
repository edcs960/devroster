"""Shared AI tools: one place for logins, models and keys; projects only choose tools."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_dev.adapters import ai_tools_service
from agent_dev.adapters.ai_providers import PROVIDERS, defaults
from agent_dev.adapters.global_ai import (
    GlobalAi,
    compose_runners,
    effective_runners,
    legacy_runtime,
    normalize_tools,
    validate_selection,
)
from agent_dev.domain.errors import DomainError, ErrorCode


def tools(**changes):
    values = {p: defaults(p) for p in PROVIDERS}
    for provider, change in changes.items():
        values[provider] = dict(values[provider], **change)
    return {"tools": values, "order": list(PROVIDERS)}


def test_normalize_fills_every_provider_and_completes_order():
    result = normalize_tools({"tools": {"claude_cli": dict(defaults("claude_cli"), enabled=True)}, "order": ["claude_cli"]})
    assert set(result["tools"]) == set(PROVIDERS)
    assert result["order"][0] == "claude_cli" and set(result["order"]) == set(PROVIDERS)
    assert result["tools"]["claude_cli"]["enabled"] is True


def test_shared_commands_are_names_or_absolute_paths():
    with pytest.raises(DomainError):
        normalize_tools(tools(codex_cli={"command": "bin/codex"}))
    assert normalize_tools(tools(codex_cli={"command": "/opt/bin/codex"}))["tools"]["codex_cli"]["command"] == "/opt/bin/codex"
    with pytest.raises(DomainError):
        normalize_tools({"tools": {"unknown_cli": {}}})


def test_revisions_are_compare_and_swap_and_immutable():
    store = GlobalAi()
    assert store.read()["revision"] == 0
    first = store.save(tools(codex_cli={"enabled": True}), 0)
    assert first["revision"] == 1
    with pytest.raises(DomainError) as error:
        store.save(tools(), 0)
    assert error.value.code == ErrorCode.STALE_VERSION
    store.save(tools(claude_cli={"enabled": True}), 1)
    assert store.read(1)["values"]["tools"]["codex_cli"]["enabled"] is True
    with sqlite3.connect(store.path) as sql, pytest.raises(sqlite3.DatabaseError):
        sql.execute("UPDATE global_ai_versions SET digest='x'")


def test_existing_v1_store_gains_ai_tables_without_losing_defaults():
    from agent_dev.adapters.global_settings import GlobalSettings

    settings = GlobalSettings()
    settings.save({"ai": {"model": "m"}}, 0)
    with sqlite3.connect(settings.path) as sql:
        sql.execute("DROP TRIGGER immutable_global_ai")
        sql.execute("DROP TRIGGER immutable_global_ai_delete")
        sql.execute("DROP TABLE global_ai_versions")
        sql.execute("DROP TABLE global_ai_auth")
        sql.execute("PRAGMA user_version=1")
    assert GlobalAi().read()["revision"] == 0  # A v1 store reads as "no shared AI".
    GlobalAi().save(tools(), 0)
    assert settings.read()["values"] == {"ai": {"model": "m"}}


def test_compose_follows_selection_and_drops_tools_switched_off():
    shared = {"values": normalize_tools(tools(codex_cli={"enabled": True}, claude_cli={"enabled": True},
                                              openai_api={"enabled": True, "model": "gpt"}))}
    rows = compose_runners(shared, ["claude_cli", "gemini_cli", "openai_api", "codex_cli"],
                           {"claude_cli": {"authenticated": True}}, {"OPENAI_API_KEY": "k"})
    assert [r["provider"] for r in rows] == ["claude_cli", "openai_api", "codex_cli"]
    assert [r["authenticated"] for r in rows] == [True, True, False]
    assert rows[1]["capabilities"] == ["text.generate"]
    assert "enabled" not in rows[0]["config"]


def test_effective_runners_need_a_choice_and_a_shared_revision():
    assert effective_runners({"runners": []}) == (None, None)
    with pytest.raises(DomainError) as error:
        effective_runners({"ai_tools": {"selected": ["codex_cli"]}})
    assert error.value.code == ErrorCode.CAPABILITY_UNAVAILABLE
    GlobalAi().save(tools(codex_cli={"enabled": True, "model": "m1"}), 0)
    runners, origin = effective_runners({"ai_tools": {"selected": ["codex_cli"]}})
    assert runners[0]["config"]["model"] == "m1" and origin["global_ai_revision"] == 1
    with pytest.raises(DomainError):
        validate_selection({"selected": ["nope"]})


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def runtime(tmp_path):
    from agent_dev.adapters.workflow_runtime import WorkflowRuntime

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    (source / "a.txt").write_text("a", encoding="utf-8")
    git(source, "add", ".")
    git(source, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(json.dumps({"orchestrator_v2": {"execute": True}}), encoding="utf-8")
    runtime = WorkflowRuntime(root)
    runtime.store.register_project(
        "P",
        "Project",
        {
            "repositories": {"source": {"path": str(source), "allowed_paths": ["a.txt"]}},
            "phases": [{"id": "developer", "capabilities": ["code.write"]}],
            "verification": {
                "commands": [{"name": "unit", "repository": "source", "command": [sys.executable, "-c", "0"]}],
                "required_gates": ["unit"],
            },
            "runners": [{"id": "gemini", "provider": "gemini_cli", "enabled": True, "config": {}}],
            "ai_tools": {"selected": ["claude_cli", "codex_cli"]},
        },
    )
    runtime.store.design("P", "D", "## Scope\nx\n## Acceptance\ny\n## Constraints\nz")
    runtime.store.approve("design", "D", 1, "user")
    return runtime


def snapshot(runtime, task):
    runtime.store.create_plan("P", "PLAN" + task, "D", 1, [{"id": task, "title": "t"}])
    runtime.store.approve("plan", "PLAN" + task, 1, "user")
    runtime.store.create_snapshot(task, runtime.resolve_snapshot(task))
    return runtime.store.snapshot(task)


def test_snapshot_uses_shared_tools_from_the_next_task_only(runtime):
    GlobalAi().save(tools(codex_cli={"enabled": True, "model": "old"}, claude_cli={"enabled": True}), 0)
    first = snapshot(runtime, "T1")
    resolved = first["resolved"]
    assert [r["provider"] for r in resolved["runners"]] == ["claude_cli", "codex_cli"]
    assert resolved["ai_settings_origin"]["global_ai_revision"] == 1
    GlobalAi().save(tools(codex_cli={"enabled": True, "model": "new"}, claude_cli={"enabled": True}), 1)
    second = snapshot(runtime, "T2")["resolved"]
    assert [r["config"]["model"] for r in second["runners"] if r["provider"] == "codex_cli"] == ["new"]
    # The approved task keeps its frozen runners and digest.
    assert runtime.store.snapshot("T1")["digest"] == first["digest"]
    assert [r["config"]["model"] for r in runtime.store.snapshot("T1")["resolved"]["runners"] if r["provider"] == "codex_cli"] == ["old"]


def test_api_keys_saved_once_reach_execution_without_entering_the_snapshot(runtime):
    GlobalAi().save(tools(openai_api={"enabled": True, "model": "gpt"}), 0)
    ai_tools_service.set_key("OPENAI_API_KEY", "sk-test-shared-value-123456")
    # The project switches its choice to the API tool (a new profile version).
    with sqlite3.connect(runtime.store.db.path) as sql:
        row = sql.execute("SELECT content_json FROM project_profile_versions WHERE project_id='P'").fetchone()
        profile = json.loads(row[0])
    profile["ai_tools"] = {"selected": ["openai_api"]}
    runtime.store.revise_profile("P", profile, 0)
    data = snapshot(runtime, "K1")
    assert "sk-test-shared-value" not in json.dumps(data)
    (runner,) = data["resolved"]["runners"]
    assert runner["provider"] == "openai_api" and runner["authenticated"] is True
    registry = runtime.runners("K1")
    assert registry.candidates[0].runner.environment["OPENAI_API_KEY"] == "sk-test-shared-value-123456"


def test_service_reports_key_presence_never_values():
    ai_tools_service.set_key("OPENAI_API_KEY", "sk-hidden-value-123456")
    text = json.dumps(ai_tools_service.state())
    assert "sk-hidden-value" not in text
    assert ai_tools_service.state()["keys"]["OPENAI_API_KEY"] is True
    ai_tools_service.set_key("OPENAI_API_KEY", delete=True)
    assert ai_tools_service.state()["keys"]["OPENAI_API_KEY"] is False


def test_legacy_runtime_follows_the_shared_tools_without_rewriting_the_file(tmp_path, monkeypatch):
    from agent_dev.adapters import project_setup

    class Setup:
        def __init__(self, root):
            pass

        def current_profile(self):
            return "p", 2, {"ai_tools": {"selected": ["claude_cli", "codex_cli"]}}

    monkeypatch.setattr(project_setup, "ProjectSetup", Setup)
    base = {"runner": "codex_cli", "setup_configuration_root": str(tmp_path),
            "codex_cli": {"workspace_path": "/w", "model": "x"}}
    assert legacy_runtime(base) is base  # No shared AI yet: keep the saved runtime.
    GlobalAi().save(tools(codex_cli={"enabled": True, "model": "shared"}, claude_cli={"enabled": True}), 0)
    result = legacy_runtime(base)
    assert result["runner"] == "claude_cli"
    assert result["runner_failover"] == {"enabled": True, "order": ["claude_cli", "codex_cli"]}
    assert result["codex_cli"]["model"] == "shared" and "enabled" not in result["codex_cli"]
    assert result["codex_cli"]["workspace_path"] == "/w"  # Project-only runtime keys survive.
    assert base["runner"] == "codex_cli"


def test_project_switch_keeps_what_it_runs(tmp_path):
    from agent_dev.adapters.project_setup import ProjectSetup, effective_ai

    project = tmp_path / "proj"
    project.mkdir()
    git(project, "init", "-q")
    (project / "f").write_text("x", encoding="utf-8")
    git(project, "add", ".")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    setup = ProjectSetup(project)
    value = setup.read()
    value["ai"]["provider"] = "claude_cli"
    value["ai"]["runners"]["claude_cli"].update(enabled=True, model="sonnet-x", effort="high")
    value["ai"]["runners"]["codex_cli"].update(enabled=True, model="gpt-y")
    setup.save(value)
    before = effective_ai(setup.read())
    plan = ai_tools_service.import_project(project)
    assert plan["creates_shared"] and plan["selected"] == ["claude_cli", "codex_cli"] and not plan["applied"]
    assert GlobalAi().read()["revision"] == 0  # Dry run writes nothing.
    done = ai_tools_service.import_project(project, apply=True)
    assert done["applied"] and done["shared_revision"] == 1
    after_value = setup.read()
    assert after_value["ai_tools"] == {"selected": ["claude_cli", "codex_cli"]}
    after = effective_ai(after_value)
    assert after["provider"] == before["provider"] == "claude_cli"
    for provider in ("claude_cli", "codex_cli"):
        for field in ("model", "effort", "command", "enabled"):
            assert after["runners"][provider][field] == before["runners"][provider][field], (provider, field)
    assert not after["runners"]["gemini_cli"]["enabled"]
    # A project that selects nothing is in preparation mode instead of running.
    after_value["ai_tools"] = {"selected": []}
    setup.save(after_value)
    assert setup.read()["preparation_mode"] is True


def test_cli_show_and_set(tmp_path, capsys):
    from agent_dev.adapters.command_line import main

    source = tmp_path / "ai.json"
    source.write_text(json.dumps(tools(codex_cli={"enabled": True})), encoding="utf-8")
    assert main(["ai", "set", str(source), "--root", str(tmp_path), "--json"]) in (0, None)
    out = json.loads(capsys.readouterr().out)
    assert out["result"]["revision"] == 1
    main(["ai", "show", "--root", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["result"]["values"]["tools"]["codex_cli"]["enabled"] is True


def test_newer_store_is_refused():
    GlobalAi().save(tools(), 0)
    with sqlite3.connect(GlobalAi().path) as sql:
        sql.execute("PRAGMA user_version=99")
    with pytest.raises(DomainError):
        GlobalAi().read()
    assert Path(GlobalAi().path).is_file()


def test_project_setup_still_opens_when_the_shared_store_is_newer(tmp_path):
    from agent_dev.adapters.project_setup import ProjectSetup

    GlobalAi().save(tools(codex_cli={"enabled": True}), 0)
    with sqlite3.connect(GlobalAi().path) as sql:
        sql.execute("PRAGMA user_version=99")
    project = tmp_path / "p"
    project.mkdir()
    value = ProjectSetup(project).read()
    value["ai_tools"] = {"selected": ["codex_cli"]}
    from agent_dev.adapters.project_setup import effective_ai

    assert effective_ai(value)["runners"]["codex_cli"]["enabled"] is False


def test_snapshot_probes_tools_nobody_has_checked(monkeypatch):
    from agent_dev.adapters import project_setup

    calls = []
    monkeypatch.setattr(project_setup, "auth_probe", lambda provider, command: calls.append(provider) or True)
    GlobalAi().save(tools(codex_cli={"enabled": True}, claude_cli={"enabled": True}), 0)
    runners, _ = effective_runners({"ai_tools": {"selected": ["claude_cli", "codex_cli"]}})
    assert calls == ["claude_cli", "codex_cli"] and all(r["authenticated"] for r in runners)
    effective_runners({"ai_tools": {"selected": ["claude_cli"]}})
    assert calls == ["claude_cli", "codex_cli"]  # Recorded results are reused, not re-probed.


def test_setup_check_follows_the_shared_tools(tmp_path, monkeypatch):
    from agent_dev.adapters import project_setup
    from agent_dev.adapters.project_setup import ProjectSetup

    monkeypatch.setattr(project_setup, "auth_probe", lambda provider, command: False)
    project = tmp_path / "proj"
    project.mkdir()
    git(project, "init", "-q")
    (project / "f").write_text("x", encoding="utf-8")
    git(project, "add", ".")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    GlobalAi().save(tools(claude_cli={"enabled": True}), 0)
    setup = ProjectSetup(project)
    value = setup.read()  # The project's own runners still enable Codex.
    value["ai_tools"] = {"selected": ["claude_cli"]}
    setup.save(value)
    names = {row["name"]: row for row in setup.check(check_auth=True)["checks"]}
    assert "claude_cli_configured_binary" in names and "claude_cli_login" in names
    assert "codex_cli_configured_binary" not in names and "codex_cli_login" not in names
    # Registered-profile runner rows are not what runs; unselected tools are not reported.
    assert not [n for n in names if n.split(":")[0] in {"auth", "api", "runner", "runner_policy"}]
    assert names["database_schema"]["status"] == "pass"
    assert names["database_schema"]["label"] == "저장소 버전"
    # Terminal logins do not reach the app-private CLI home.
    assert "DevRoster 전용 로그인" in names["claude_cli_login"]["action"]
    assert names["claude_cli_login"]["label"] == "Claude CLI 로그인"


def test_root_opens_setup_until_the_project_is_saved(tmp_path):
    import http.client
    import threading

    from agent_dev.adapters.project_setup import ProjectSetup
    from agent_dev.ui.studio_server import make_server

    project = tmp_path / "proj"
    project.mkdir()
    git(project, "init", "-q")
    (project / "f").write_text("x", encoding="utf-8")
    git(project, "add", ".")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    server = make_server(project, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def location():
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request("GET", "/")
        response = connection.getresponse()
        connection.close()
        return response.status, response.getheader("Location")

    try:
        assert location() == (302, "/setup")
        setup = ProjectSetup(project)
        setup.save(setup.read())
        assert location() == (302, "/agent")
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()
        thread.join()


def test_new_project_starts_on_the_shared_tools_and_pickers_show_their_state(tmp_path):
    from agent_dev.adapters.project_setup import ProjectSetup, ai_tool_notes

    project = tmp_path / "proj"
    project.mkdir()
    git(project, "init", "-q")
    # Without shared tools a new project keeps its own runners.
    assert ProjectSetup(project).read()["ai_tools"] is None
    GlobalAi().save(tools(claude_cli={"enabled": True}, codex_cli={"enabled": True}), 0)
    GlobalAi().record_auth({"claude_cli": False, "codex_cli": True})
    value = ProjectSetup(project).read()
    assert value["ai_tools"] == {"selected": ["codex_cli", "claude_cli"]}
    assert value["preparation_mode"] is False
    notes = ai_tool_notes(project)
    assert notes["shared"] is True
    assert notes["notes"]["claude_cli"] == "로그인 필요"
    assert "codex_cli" not in notes["notes"]
    assert notes["notes"]["gemini_cli"] == "이 프로젝트에서 사용 안 함"


def test_setup_save_does_not_probe_runners_replaced_by_shared_tools(tmp_path, monkeypatch):
    # The setup page hides the login-check option once shared tools are selected; saving must not
    # launch logins for the project's own runners, which no longer run.
    from agent_dev.adapters import project_setup
    from agent_dev.adapters.project_setup import ProjectSetup

    calls = []
    monkeypatch.setattr(
        project_setup, "auth_probe", lambda provider, command: calls.append(provider) or True
    )
    project = tmp_path / "proj"
    project.mkdir()
    git(project, "init", "-q")
    (project / "f").write_text("x", encoding="utf-8")
    git(project, "add", ".")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    GlobalAi().save(tools(claude_cli={"enabled": True}), 0)
    setup = ProjectSetup(project)
    value = setup.read()
    value["ai_tools"] = {"selected": ["claude_cli"]}
    setup.save(value, check_auth=True)
    assert calls == []
    own = setup.read()
    own.pop("ai_tools", None)
    setup.save(own, check_auth=True)  # The project's own runners: the check still runs.
    assert calls
