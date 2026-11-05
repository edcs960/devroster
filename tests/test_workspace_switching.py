"""Two independent roots opened through the same loopback application."""

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_project_setup import config, request

from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.workspace_registry import project_list, register_root, roots
from agent_dev.app import remembered_root
from agent_dev.ui.setup_server import make_server as make_setup_server
from agent_dev.ui.studio_server import make_server


@contextmanager
def running(root, factory=make_server):
    server = factory(root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()
        thread.join(timeout=5)


def decode(base, path, payload=None, token=""):
    status, body = request(base, path, payload, token)
    return status, json.loads(body)


@pytest.mark.parametrize("factory", [make_server, make_setup_server])
def test_switch_separates_data_and_reuses_sessions(tmp_path, monkeypatch, factory):
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "app"))
    a, b = tmp_path / "a", tmp_path / "b"
    for root, name in [(a, "Alpha"), (b, "Beta")]:
        value = config(root)
        value["name"] = name
        value["environment"] = [{"key": "ONLY_HERE", "value": name + "-private"}]
        ProjectSetup(root).save(value)
    with running(a, factory) as (server, base):
        status, listing = decode(base, "/api/setup/projects")
        assert status == 200
        token = listing["token"]
        assert listing["active_root"] == str(a)
        assert decode(base, "/api/setup/project-open", {"root": str(b)}, token)[0] == 400
        assert decode(base, "/api/setup/project-register", {"root": str(b)}, token)[0] == 200
        _, opened = decode(base, "/api/setup/project-open", {"root": str(b)}, token)
        destination = urlsplit(opened["url"])
        beta = f"{destination.scheme}://{destination.netloc}"
        assert destination.hostname == "127.0.0.1"
        assert destination.path == "/agent"
        assert beta != base
        assert decode(beta, "/api/setup/bootstrap")[1]["settings"]["name"] == "Beta"
        assert decode(base, "/api/setup/bootstrap")[1]["settings"]["name"] == "Alpha"
        # Same project identifiers/documents cannot cross the server/DB boundary.
        server.studio.studio.create("Only Alpha", "Alpha document")
        child = server.studio.workspace_sessions.servers[str(b)]
        child.studio.studio.create("Only Beta", "Beta document")
        assert [p["title"] for p in decode(base, "/api/studio/bootstrap")[1]["projects"]] == [
            "Only Alpha"
        ]
        assert [p["title"] for p in decode(beta, "/api/studio/bootstrap")[1]["projects"]] == [
            "Only Beta"
        ]
        assert (
            decode(base, "/api/setup/project-open", {"root": str(b)}, token)[1]["url"]
            == opened["url"]
        )
        beta_token = decode(beta, "/api/setup/projects")[1]["token"]
        assert (
            decode(beta, "/api/setup/project-open", {"root": str(a)}, beta_token)[1]["url"]
            == base + "/agent"
        )
        assert remembered_root() == a
        assert len(server.studio.workspace_sessions.children) == 1
        assert decode(beta, "/api/setup/project-open", {"root": str(a)}, token)[0] == 403
        assert decode(base, "/api/setup/project-register", {"root": str(b)})[0] == 403
        assert (
            decode(base, "/api/setup/project-open", {"root": "https://example.com"}, token)[0]
            == 400
        )
        assert "Alpha-private" not in json.dumps(decode(base, "/api/setup/projects")[1])
        manager = server.studio.workspace_sessions
    assert manager.closed and not manager.children


def test_registry_legacy_atomic_dedup_missing_and_invalid(tmp_path, monkeypatch):
    home = tmp_path / "app"
    monkeypatch.setenv("DEVROSTER_HOME", str(home))
    home.mkdir()
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (home / "app.json").write_text(json.dumps({"root": str(a), "unrelated": "preserved"}))
    register_root(b)
    register_root(b / ".." / "b")
    assert set(roots()) == {str(a), str(b)}
    assert json.loads((home / "app.json").read_text())["unrelated"] == "preserved"
    b.rmdir()
    listing = project_list(a)
    assert next(row for row in listing["projects"] if row["root"] == str(b))["available"] is False
    with pytest.raises(ValueError):
        register_root(tmp_path / "missing")
    with running(a) as (_, base):
        token = decode(base, "/api/setup/projects")[1]["token"]
        assert decode(base, "/api/setup/project-open", {"root": str(b)}, token)[0] == 400
    assert not b.exists()


def test_new_project_save_registers_and_unprepared_open_goes_to_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "app"))
    a, b = tmp_path / "a", tmp_path / "new"
    a.mkdir()
    with running(a) as (_, base):
        token = decode(base, "/api/setup/projects")[1]["token"]
        value = config(b)
        assert not b.exists()
        assert (
            decode(
                base,
                "/api/setup/save",
                {"root": str(b), "settings": value, "check_auth": False},
                token,
            )[0]
            == 200
        )
        assert str(b) in roots()
        blank = tmp_path / "blank"
        blank.mkdir()
        decode(base, "/api/setup/project-register", {"root": str(blank)}, token)
        assert decode(base, "/api/setup/project-open", {"root": str(blank)}, token)[1][
            "url"
        ].endswith("/setup")
        assert not (blank / ".agent-dev").exists()


def test_workspace_picker_browser(tmp_path, monkeypatch):
    import os
    import subprocess

    module = os.environ.get("AGENT_DEV_TEST_PLAYWRIGHT_MODULE")
    executable = os.environ.get("AGENT_DEV_TEST_BROWSER_EXECUTABLE")
    if not module or not executable:
        pytest.skip("Opt in with installed Playwright and browser; no installation.")
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "app"))
    a = tmp_path / "alpha"
    a.mkdir()
    with running(a) as (_, base):
        result = subprocess.run(
            [
                "node",
                str(Path(__file__).with_name("workspace_picker_browser.cjs")),
                base,
                str(tmp_path),
                module,
                executable,
                str(tmp_path / "browser"),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=100,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_local_navigation_does_not_relax_api_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "app"))
    with running(tmp_path) as (_, base):
        headers = {
            "Sec-Fetch-Site": "same-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }
        assert request(base, "/setup", headers=headers)[0] == 200
        assert request(base, "/agent", headers=headers)[0] == 200
        assert request(base, "/studio", headers=headers)[0] == 200
        assert request(base, "/api/setup/projects", headers=headers)[0] == 403
        assert request(base, "/api/agent/bootstrap", headers=headers)[0] == 403
        assert request(base, "/setup", headers={**headers, "Sec-Fetch-Dest": "iframe"})[0] == 403
        assert (
            request(base, "/setup", headers={**headers, "Sec-Fetch-Site": "cross-site"})[0] == 403
        )


def test_legacy_configuration_root_is_preserved(tmp_path, monkeypatch):
    from agent_dev.adapters.workspace_registry import configuration_for
    from agent_dev.app import resolve_root
    from agent_dev.core import paths

    root = tmp_path / "legacy"
    engine = root / "engine"
    (engine / "config").mkdir(parents=True)
    (engine / "config/runtime.json").write_text(json.dumps({"system_name": "Legacy"}))
    monkeypatch.setattr(paths, "AGENT_DEV_ROOT", engine)
    monkeypatch.setattr(paths, "PROJECT_ROOT", root)
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "app"))
    register_root(engine, remember=True)
    assert roots() == [str(root)]
    assert configuration_for(root) == engine
    assert resolve_root(None) == engine
    rows = project_list(engine)["projects"]
    assert len(rows) == 1 and rows[0]["configuration_root"] == str(engine)


def test_concurrent_registration_and_failed_start_leave_current_intact(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from agent_dev.ui import studio_server

    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "app"))
    folders = [tmp_path / str(i) for i in range(5)]
    for p in folders:
        p.mkdir()
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(register_root, folders))
    assert set(roots()) == set(map(str, folders))
    with running(folders[0]) as (server, base):
        manager = server.studio.workspace_sessions
        monkeypatch.setattr(
            studio_server,
            "make_server",
            lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic bind failure")),
        )
        with pytest.raises(OSError):
            manager.open(str(folders[1]), folders[0])
        assert list(manager.servers.values()) == [server]
        assert decode(base, "/api/setup/bootstrap")[0] == 200
