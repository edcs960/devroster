"""Desktop app behaviour: quit from the screen, one running copy, fast local server start."""

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from agent_dev import app
from agent_dev.ui.local_server import LocalHTTPServer
from agent_dev.ui.studio_server import make_server


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "home"))
    instance = make_server(tmp_path / "project", 0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.studio.close()
    instance.server_close()
    thread.join(timeout=5)


def call(instance, path, method="GET", headers=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{instance.server_port}{path}", method=method, headers=headers or {}, data=b"" if method == "POST" else None
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_quit_is_offered_only_by_the_desktop_launcher(server):
    assert call(server, "/api/app/status")[1]["can_quit"] is False
    assert call(server, "/api/app/quit", "POST", {"X-DevRoster-Action": "quit"})[0] == 404
    stopped = threading.Event()
    server.studio.quit = stopped.set
    assert call(server, "/api/app/status")[1]["can_quit"] is True
    # A plain form post cannot add the custom header; a cross-site page is refused before that.
    assert call(server, "/api/app/quit", "POST")[0] == 403
    assert call(server, "/api/app/quit", "POST", {"X-DevRoster-Action": "quit", "Origin": "https://evil.test"})[0] == 403
    assert not stopped.is_set()
    assert call(server, "/api/app/quit", "POST", {"X-DevRoster-Action": "quit"}) == (200, {"stopping": True})
    assert stopped.wait(5)


def test_second_launch_finds_the_running_copy(server, monkeypatch):
    server.studio.quit = lambda: None
    app.record_instance(server.server_port)
    assert app.running_instance() == f"http://127.0.0.1:{server.server_port}"
    app.clear_instance()
    assert not (app.app_home() / app.INSTANCE_FILE).exists()
    monkeypatch.setattr(app, "DEFAULT_PORT", server.server_port)
    # Still starting (no instance file yet): the default port answers.
    assert app.running_instance() == f"http://127.0.0.1:{server.server_port}"


def test_no_running_copy_when_nothing_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "home"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
    monkeypatch.setattr(app, "DEFAULT_PORT", free)
    app.record_instance(free)
    assert app.running_instance() is None


def test_clear_instance_keeps_another_process_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "home"))
    path = app.app_home() / app.INSTANCE_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"port": 1, "pid": -1}), encoding="utf-8")
    app.clear_instance()
    assert path.exists()


def test_packaged_app_finds_cli_folders():
    import os

    shell = os.pathsep.join(["/opt/homebrew/bin", "/usr/bin"])
    assert app.packaged_path("/usr/bin:/bin".replace(":", os.pathsep), shell).split(os.pathsep) == [
        "/opt/homebrew/bin",
        "/usr/bin",
        "/bin",
    ]
    fallback = app.packaged_path("/usr/bin", None).split(os.pathsep)
    assert fallback[0] == "/usr/bin" and "/opt/homebrew/bin" in fallback


def test_local_server_does_not_wait_for_reverse_dns(monkeypatch):
    def slow_lookup(*args):
        raise AssertionError("getfqdn must not be called")

    monkeypatch.setattr(socket, "getfqdn", slow_lookup)
    from http.server import BaseHTTPRequestHandler

    instance = LocalHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        assert instance.server_name == "127.0.0.1" and instance.server_port > 0
    finally:
        instance.server_close()
