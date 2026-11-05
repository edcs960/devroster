import pytest

from agent_dev.adapters.integration_catalog import catalog
from agent_dev.adapters.integrations import Integrations
from agent_dev.domain.errors import DomainError


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
