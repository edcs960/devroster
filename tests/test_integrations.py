import os
import sys
import typing

import pytest

from agent_dev.adapters.integration_catalog import catalog
from agent_dev.adapters.integrations import Integrations
from agent_dev.domain.errors import DomainError, ErrorCode


def test_optional_catalog_complete():
    rows = catalog()
    assert len(rows) == 22
    assert len({row["id"] for row in rows}) == 22
    assert {"graphify", "serena", "context-mode", "figma", "stitch", "rea"} <= {r["id"] for r in rows}


def test_rea_allows_only_static_analysis_tools():
    rea = next(row for row in catalog() if row["id"] == "rea")
    assert rea["command"] == ["rea", "mcp"] and rea["transport"] == "stdio"
    tools = set(rea["operations"])
    assert "analyze_javascript_application" in tools and "inspect_native_exports" in tools
    # Device, instrumentation and debugger tools, and tools that need an earlier session.
    for excluded in (
        "install_adb_package",
        "instrument_with_frida",
        "execute_rizin_command",
        "start_gdb_session",
        "binary_overview",
        "analyze_web_bundle",
        "compare_application_versions",
    ):
        assert excluded not in tools


def test_connections_are_optional_and_export_secret_free(tmp_path):
    app = Integrations(tmp_path)
    assert app.list() == []
    row = app.configure(
        {"id": "docs", "provider": "context7", "scopes": ["react"], "credential_ref": "DOC_TOKEN"}
    )
    assert row["state"] == "configured"
    exported = app.export()
    assert "DOC_TOKEN" not in str(exported)
    assert exported["connections"][0]["enabled"] is False
    assert app.import_document(exported)["imported"] == 1


def test_unregistered_and_unknown_operations_are_denied(tmp_path):
    app = Integrations(tmp_path)
    with pytest.raises(DomainError):
        app.call("absent", "read", {})
    app.configure({"id": "design", "provider": "figma", "scopes": ["AbC"], "transport": "api"})
    with pytest.raises(DomainError):
        app.call("design", "write", {}, scope="AbC")


def test_secrets_and_unsafe_remote_are_not_configurable(tmp_path):
    app = Integrations(tmp_path)
    for changes in [
        {"token": "secret"},
        {"endpoint": "https://127.0.0.1/mcp"},
        {"id": "../escape"},
    ]:
        with pytest.raises(DomainError):
            app.configure(
                dict({"id": "docs", "provider": "context7", "scopes": ["react"]}, **changes)
            )


def test_optimistic_connection_save_and_project_copy(tmp_path):
    app = Integrations(tmp_path)
    app.configure({"id": "docs", "provider": "context7", "scopes": ["react"]})
    with pytest.raises(DomainError):
        app.configure(
            {"id": "docs", "provider": "context7", "scopes": ["react"]}, expected_version=0
        )
    copied = Integrations(tmp_path / "other")
    copied.import_document(app.export())
    assert copied.list()[0]["state"] == "configured"
    assert copied.list()[0]["scopes"] == []



class FakeMcp:
    started: typing.ClassVar[list] = []

    def __init__(self, config, **options):
        FakeMcp.started.append(config["command"])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def tools(self):
        return []


def stdio_app(tmp_path, command):
    FakeMcp.started = []
    app = Integrations(tmp_path, client_factory=FakeMcp)
    app.configure(
        {
            "id": "local",
            "provider": "custom",
            "name": "로컬 서버",
            "transport": "stdio",
            "command": command,
            "scopes": ["docs"],
            "operations": {"read": {"tool": "read", "capability": "docs.read"}},
        }
    )
    return app


def test_stdio_server_starts_only_after_its_launch_command_is_confirmed(tmp_path):
    """Audit S-06: the launcher name list lets `python3 -c` through, so confirmation is the gate."""
    app = stdio_app(tmp_path, [sys.executable, "-c", "print('anything')"])
    launch = app.launch("local")
    assert launch["executable"] == os.path.abspath(sys.executable) and launch["found"]
    assert launch["arguments"] == ["-c", "print('anything')"] and not launch["approved"]
    row = app.check("local")
    assert row["state"] == "approval_required" and FakeMcp.started == []
    with pytest.raises(DomainError):  # no completed read check yet, so a call never reaches the server
        app.call("local", "read", {}, scope="docs")
    with pytest.raises(DomainError) as error:
        app._client(app.show("local"))
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED and FakeMcp.started == []
    with pytest.raises(DomainError) as error:
        app.approve_launch("local", "0" * 64)
    assert error.value.code == ErrorCode.STALE_VERSION
    assert app.approve_launch("local", launch["digest"])["approved"]
    assert app.check("local")["state"] != "approval_required" and len(FakeMcp.started) == 1


def test_changing_the_command_asks_for_confirmation_again(tmp_path):
    app = stdio_app(tmp_path, [sys.executable, "-c", "pass"])
    app.approve_launch("local", app.launch("local")["digest"])
    row = app.show("local")
    for change in (
        {"command": [sys.executable, "-c", "import os"]},
        {"command": [sys.executable, "-c", "pass"], "environment_keys": ["EXTRA_KEY"]},
    ):
        config = {k: row[k] for k in ("id", "provider", "name", "transport", "scopes", "operations")} | change
        row = app.configure(config, row["version"])
        assert not app.launch("local")["approved"]
        assert app.check("local")["state"] == "approval_required"
        row = app.show("local")
    assert FakeMcp.started == []


def test_missing_executable_cannot_be_confirmed(tmp_path):
    app = stdio_app(tmp_path, ["devroster-no-such-mcp-server"])
    launch = app.launch("local")
    assert not launch["found"] and not launch["approved"]
    with pytest.raises(DomainError):
        app.approve_launch("local", launch["digest"])
    assert app.check("local")["state"] == "installation_required"


def test_confirmation_is_stored_outside_the_project(tmp_path):
    from agent_dev.adapters import workspace_trust

    project = tmp_path / "project"
    project.mkdir()
    app = stdio_app(project, [sys.executable, "-c", "pass"])
    app.approve_launch("local", app.launch("local")["digest"])
    stored = workspace_trust._path()
    assert project.resolve() not in stored.resolve().parents
    assert "local" in stored.read_text(encoding="utf-8")
    for path in project.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".txt"}:
            assert app.launch("local")["digest"] not in path.read_text(encoding="utf-8", errors="replace")



def test_web_page_shows_and_records_the_launch_confirmation(tmp_path):
    from agent_dev.ui.integration_server import IntegrationWeb

    stdio_app(tmp_path, [sys.executable, "-c", "pass"])
    web = IntegrationWeb(tmp_path)
    row = next(item for item in web.get("bootstrap", {})["connections"] if item["id"] == "local")
    assert row["launch_approved"] is False
    launch = web.get("launch", {"id": "local"})
    assert launch["arguments"] == ["-c", "pass"] and not launch["approved"]
    with pytest.raises(DomainError):
        web.post("approve-launch", {"id": "local", "digest": "stale"})
    assert web.post("approve-launch", {"id": "local", "digest": launch["digest"]})["approved"]
    row = next(item for item in web.get("bootstrap", {})["connections"] if item["id"] == "local")
    assert row["launch_approved"] is True


def test_cli_shows_the_launch_and_confirms_with_the_digest(tmp_path):
    from agent_dev.adapters.command_line import execute

    stdio_app(tmp_path, [sys.executable, "-c", "pass"])
    base = ["integration", "launch", "local", "--root", str(tmp_path)]
    _args, shown, rejected = execute(base)
    assert not rejected and shown["result"]["approved"] is False
    _args, _wrong, rejected = execute([*base, "--confirm-digest", "stale"])
    assert rejected and Integrations(tmp_path).launch("local")["approved"] is False
    _args, done, rejected = execute([*base, "--confirm-digest", shown["result"]["digest"]])
    assert not rejected and done["result"]["approved"] is True
