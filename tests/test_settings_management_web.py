"""Shared settings, inheritance and storage moves through the loopback setup server and CLI."""

import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_project_setup import request, server
from test_reusable_workspace import workspace  # noqa: F401

from agent_dev.adapters.command_line import main
from agent_dev.adapters.workspace import configuration_root

SECRET = "settings-web-private-value"


@contextmanager
def setup_server(root):
    with server(root) as base:
        status, body = request(base, "/api/settings/global")
        assert status == 200
        yield base, json.loads(body)["token"]


def call(base, path, payload=None, token=""):
    status, body = request(base, path, payload, token)
    return status, json.loads(body)


def test_global_override_inheritance_and_origins(workspace):  # noqa: F811
    (workspace / ".agent-dev/environment.json").write_text(json.dumps({"APP_TOKEN": SECRET}), encoding="utf-8")
    root = str(workspace)
    with setup_server(workspace) as (base, token):
        status, shared = call(base, "/api/settings/global")
        assert status == 200 and shared["settings"]["revision"] == 0
        assert shared["location"]["management_source"] == "environment"
        values = {"ai": {"provider": "claude_cli", "model": "shared-model"}}
        assert (
            call(base, "/api/settings/global", {"values": values, "expected_revision": 0})[0] == 403
        )
        status, saved = call(
            base, "/api/settings/global", {"values": values, "expected_revision": 0}, token
        )
        assert status == 200 and saved["revision"] == 1
        stale = call(
            base, "/api/settings/global", {"values": values, "expected_revision": 0}, token
        )
        assert stale[0] == 409
        status, effective = call(base, "/api/settings/effective?root=" + root)
        assert status == 200
        assert effective["origins"]["ai.model"] == "global"
        assert effective["global_revision"] == 1 and effective["pending_global"] is True
        revision = effective["project_revision"]
        status, applied = call(
            base,
            "/api/settings/apply-global",
            {
                "root": root,
                "global_revision": 1,
                "expected_revision": revision,
                "overrides": {"ai": {"model": ""}},
            },
            token,
        )
        assert status == 200, applied
        status, effective = call(base, "/api/settings/effective?root=" + root)
        assert effective["origins"]["ai.model"] == "project"
        assert effective["applied"]["global_revision"] == 1
        status, applied = call(
            base,
            "/api/settings/apply-global",
            {
                "root": root,
                "global_revision": 1,
                "expected_revision": effective["project_revision"],
                "overrides": {},
            },
            token,
        )
        assert status == 200
        status, effective = call(base, "/api/settings/effective?root=" + root)
        assert effective["origins"]["ai.model"] == "global"
        assert effective["global_revision"] == 1
        stale = call(
            base,
            "/api/settings/apply-global",
            {"root": root, "global_revision": 1, "expected_revision": 0},
            token,
        )
        assert stale[0] == 409
        bad = call(
            base,
            "/api/settings/apply-global",
            {
                "root": root,
                "global_revision": 1,
                "expected_revision": effective["project_revision"],
                "overrides": {"ai": {"model": None}},
            },
            token,
        )
        assert bad[0] == 400
        for path in ("/api/settings/global", "/api/settings/effective?root=" + root):
            assert SECRET not in json.dumps(call(base, path)[1])
            assert "credential_value" not in json.dumps(call(base, path)[1])


def test_shared_connections_are_registered_bound_and_disabled(workspace):  # noqa: F811
    root = str(workspace)
    with setup_server(workspace) as (base, token):
        definition = {"provider": "github", "scopes": ["repo-A"], "credential_ref": "GH_TOKEN"}
        status, row = call(
            base,
            "/api/settings/connections",
            {"connection_id": "gh", "config": definition, "expected_version": 0},
            token,
        )
        assert status == 200 and row["version"] == 1
        status, bound = call(
            base,
            "/api/settings/connections/bind",
            {
                "root": root,
                "local_id": "github",
                "connection_id": "gh",
                "version": 1,
                "scopes": ["repo-A"],
                "operations": ["get_me"],
                "expected_version": 0,
            },
            token,
        )
        assert status == 200 and bound["shared"]["connection_id"] == "gh"
        widened = call(
            base,
            "/api/settings/connections/bind",
            {
                "root": root,
                "local_id": "github",
                "connection_id": "gh",
                "version": 1,
                "scopes": ["repo-B"],
                "operations": ["get_me"],
                "expected_version": bound["version"],
            },
            token,
        )
        assert widened[0] == 400
        status, listing = call(base, "/api/settings/global")
        assert [c["connection_id"] for c in listing["connections"]] == ["gh"]
        status, disabled = call(
            base,
            "/api/settings/connections/disable",
            {"connection_id": "gh", "expected_revision": 1},
            token,
        )
        assert status == 200 and disabled["enabled"] is False
        assert (
            call(
                base,
                "/api/settings/connections/disable",
                {"connection_id": "gh", "expected_revision": 1},
                token,
            )[0]
            == 409
        )


def test_storage_plan_apply_and_recover_over_http(workspace, tmp_path):  # noqa: F811
    target = tmp_path / "managed"
    with setup_server(workspace) as (base, token):
        status, plan = call(
            base,
            "/api/settings/storage/plan",
            {"source": str(workspace), "destination": str(target), "scope": "workspace"},
            token,
        )
        assert status == 200 and plan["ready"], plan
        assert not target.exists()
        status, _ = call(
            base,
            "/api/settings/storage/apply",
            {"plan_id": plan["plan_id"], "expected_registry_revision": 9},
            token,
        )
        assert status == 409
        status, applied = call(
            base,
            "/api/settings/storage/apply",
            {"plan_id": plan["plan_id"], "expected_registry_revision": plan["registry_revision"]},
            token,
        )
        assert status == 200 and applied["state"] == "completed" and applied["restart_required"]
        assert configuration_root(workspace) == target
        status, recovered = call(
            base,
            "/api/settings/storage/recover",
            {"migration_id": plan["plan_id"], "action": "rollback"},
            token,
        )
        assert status == 200 and recovered["state"] == "rolled_back"
        assert configuration_root(workspace) == workspace / ".agent-dev"
        assert (
            call(
                base,
                "/api/settings/storage/recover",
                {"migration_id": "not-an-id", "action": "resume"},
                token,
            )[0]
            == 400
        )


def test_settings_page_assets_are_served(workspace):  # noqa: F811
    with setup_server(workspace) as (base, _):
        status, body = request(base, "/settings")
        assert status == 200 and b'id="global-form"' in body
        assert request(base, "/settings.js")[0] == 200
        status, body = request(base, "/setup")
        assert b'href="/settings"' in body


def cli(capsys, *args):
    assert main([*args, "--json"]) in {0, None}
    return json.loads(capsys.readouterr().out)["result"]


def test_cli_location_show_plan_apply_recover(workspace, tmp_path, capsys):  # noqa: F811
    shown = cli(capsys, "settings", "location", "show", "--root", str(workspace))
    assert shown["workspace"]["config_root"] == str(workspace / ".agent-dev")
    assert shown["management_source"] == "environment"
    target = tmp_path / "managed"
    plan = cli(
        capsys,
        "settings",
        "location",
        "plan",
        str(target),
        "--scope",
        "workspace",
        "--root",
        str(workspace),
    )
    assert plan["ready"]
    applied = cli(
        capsys, "settings", "location", "apply", plan["plan_id"], "--root", str(workspace)
    )
    assert applied["state"] == "completed"
    shown = cli(capsys, "settings", "location", "show", "--root", str(workspace))
    assert shown["workspace"]["config_root"] == str(target)
    assert shown["workspace"]["layout"] == "external"
    recovered = cli(
        capsys,
        "settings",
        "location",
        "recover",
        plan["plan_id"],
        "--action",
        "rollback",
        "--root",
        str(workspace),
    )
    assert recovered["state"] == "rolled_back"


def test_settings_browser(workspace, tmp_path, monkeypatch):  # noqa: F811
    module = os.environ.get("AGENT_DEV_TEST_PLAYWRIGHT_MODULE")
    executable = os.environ.get("AGENT_DEV_TEST_BROWSER_EXECUTABLE")
    if not module or not executable or not shutil.which("node"):
        pytest.skip("Opt in with installed Playwright and browser; no installation.")
    (tmp_path / "moved").mkdir()
    with server(workspace) as base:
        result = subprocess.run(
            [
                "node",
                str(Path(__file__).with_name("settings_management_browser.cjs")),
                base,
                str(workspace),
                str(tmp_path / "moved"),
                module,
                executable,
                str(tmp_path / "browser"),
            ],
            capture_output=True,
            check=False,
            text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
