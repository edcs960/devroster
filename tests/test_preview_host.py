"""Registered app preview (RV-02): owned processes, readiness, captures, comments."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from agent_dev.adapters import preview_host
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.errors import DomainError
from agent_dev.ui.local_web import make_server
from agent_dev.ui.review_server import ReviewWeb

APP = r'''
import http.server, os, subprocess, sys
port, pidfile = int(sys.argv[1]), sys.argv[2]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(pidfile, "w").write(f"{os.getpid()} {child.pid}")
PAGE = b"""<!doctype html><html><body><h1 data-testid="title">Preview fixture</h1>
<button data-testid="save">Save</button><input type="password" value="hunter2-value" aria-label="pw">
<img src="http://external.invalid/x.png"></body></html>"""
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "http://external.invalid/"); self.end_headers(); return
        body = b"ok" if self.path == "/health" else PAGE
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # zombie children of a still-running parent are not alive
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
        return bool(state) and not state.startswith("Z")
    except OSError:
        return True


def wait_dead(pids, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline and any(alive(p) for p in pids):
        time.sleep(0.2)
    return not any(alive(p) for p in pids)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def preview(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/app.py").write_text("x = 1\n")
    git(source, "init", "-q")
    git(source, "add", "-A")
    git(source, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "base")
    app = tmp_path / "fixture_app.py"
    app.write_text(APP)
    port, pidfile = free_port(), tmp_path / "pids.txt"
    services = [
        {"id": "web", "module": "source", "argv": [sys.executable, str(app), str(port), str(pidfile)], "port": port, "readiness_path": "/health", "startup_timeout": 20},
        {"id": "moved", "module": "source", "argv": [sys.executable, str(app), str(port), str(pidfile)], "port": port, "readiness_path": "/redirect", "startup_timeout": 5},
    ]
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(json.dumps({"orchestrator_v2": {"execute": True}}))
    runtime = WorkflowRuntime(root)
    runtime.store.register_project(
        "P",
        "Project",
        {
            "repositories": {"source": {"path": str(source), "allowed_paths": ["src"]}},
            "phases": [{"id": "developer", "capabilities": ["code.write"]}],
            "verification": {"commands": [], "required_gates": []},
            "preview_services": services,
        },
    )
    runtime.store.design("P", "D", "## Scope\nS\n## Acceptance\nA\n## Constraints\nC")
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan("P", "PLAN", "D", 1, [{"id": "T", "title": "Preview task"}])
    runtime.store.approve("plan", "PLAN", 1, "user")
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    web = ReviewWeb(root)
    server = make_server(web)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    started = []
    yield {"runtime": runtime, "server": server, "web": web, "port": port, "app": app, "pidfile": pidfile, "started": started}
    web.close()
    server.shutdown()
    server.server_close()
    for process in started:
        process.kill()
        process.wait()


def call(server, path, payload=None, token=None, raw=False):
    headers = {"Host": f"127.0.0.1:{server.server_port}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers.update({"Content-Type": "application/json", "X-Local-Token": token or ""})
    request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return response.status, body if raw else json.loads(body)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def prepare_and_start(p, mode, service="web"):
    _, prepared = call(p["server"], "/api/review/preview-prepare", {"task": "T", "service": service}, p["web"].token)
    return call(p["server"], "/api/review/preview-start", {"task": "T", "service": service, "mode": mode, "confirm_digest": prepared.get("confirm_digest")}, p["web"].token)


def pids(p):
    return [int(x) for x in p["pidfile"].read_text().split()]


def test_profile_validation_rejects_unsafe_services():
    repos = {"source": {}}
    base = {"id": "web", "module": "source", "argv": ["npm", "run", "dev"], "port": 5173}
    assert preview_host.normalize_services([base], repos)[0]["host"] == "127.0.0.1"
    for change in (
        {"argv": "npm run dev; rm -rf /"},
        {"host": "0.0.0.0"},
        {"port": 80},
        {"module": "unknown"},
        {"cwd": "../outside"},
        {"readiness_path": "//evil"},
        {"allowed_origins": ["https://cdn.example.com"]},
        {"env_refs": ["LD_PRELOAD"]},
    ):
        with pytest.raises(DomainError):
            preview_host.normalize_services([dict(base, **change)], repos)
    with pytest.raises(DomainError):
        preview_host.normalize_services([dict(base, argv=["run", "--token=supersecret-value"])], repos, ("supersecret-value",))


def test_launch_owns_process_tree_and_stop_ends_it(preview):
    p = preview
    status, session = prepare_and_start(p, "launch")
    assert status == 200 and session["owned"] is True, session
    parent, child = pids(p)
    assert alive(parent) and alive(child)
    status, result = call(p["server"], "/api/review/preview-stop", {"task": "T", "session_id": session["session_id"]}, p["web"].token)
    assert status == 200 and result["process_stopped"] is True
    assert wait_dead([parent, child])


def test_lease_expiry_ends_owned_process(preview):
    p = preview
    _, session = prepare_and_start(p, "launch")
    parent, child = pids(p)
    with p["runtime"].store.db.transaction() as conn:
        conn.execute("UPDATE leases SET expires_at=0 WHERE resource LIKE 'preview:%'")
    assert wait_dead([parent, child])
    status, _ = call(p["server"], "/api/review/preview-capture", {"task": "T", "session_id": session["session_id"], "generation": session["generation"]}, p["web"].token)
    assert status == 409


def test_connect_never_owns_and_occupied_port_blocks_launch(preview):
    p = preview
    own = subprocess.Popen([sys.executable, str(p["app"]), str(p["port"]), str(p["pidfile"])])
    p["started"].append(own)
    deadline = time.time() + 10
    while time.time() < deadline and not p["pidfile"].exists():
        time.sleep(0.1)
    time.sleep(0.5)
    status, result = prepare_and_start(p, "launch")
    assert status == 400 and "사용 중" in result["error"]
    status, session = prepare_and_start(p, "connect")
    assert status == 200 and session["owned"] is False
    call(p["server"], "/api/review/preview-stop", {"task": "T", "session_id": session["session_id"]}, p["web"].token)
    assert own.poll() is None


def test_redirecting_readiness_and_stale_confirmation_are_rejected(preview):
    p = preview
    status, result = prepare_and_start(p, "launch", "moved")
    assert status == 400 and "이동" in result["error"]
    status, result = call(p["server"], "/api/review/preview-start", {"task": "T", "service": "web", "mode": "launch", "confirm_digest": "0" * 64}, p["web"].token)
    assert status == 409
    status, result = call(p["server"], "/api/review/preview-prepare", {"task": "T", "service": "unknown"}, p["web"].token)
    assert status == 400


def test_agent_ui_port_is_never_a_preview_origin(preview):
    p = preview
    context = preview_host.PreviewContext(p["runtime"].store.db, "T")
    with pytest.raises(DomainError):
        preview_host.prepare(context, {"service": "web"}, p["port"])


def test_capture_without_browser_reports_unavailable(preview, monkeypatch):
    p = preview
    for key in preview_host.BROWSER_KEYS:
        monkeypatch.delenv(key, raising=False)
    _, session = prepare_and_start(p, "launch")
    status, result = call(p["server"], "/api/review/preview-capture", {"task": "T", "session_id": session["session_id"], "generation": session["generation"]}, p["web"].token)
    assert status == 200 and result["available"] is False and "자동 설치" in result["reason"]


def test_get_routes_do_not_write_and_posts_need_token(preview):
    import hashlib

    p = preview
    database = p["runtime"].store.db.path
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    status, data = call(p["server"], "/api/review/preview?task=T")
    assert status == 200 and [s["id"] for s in data["services"]] == ["web", "moved"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert call(p["server"], "/api/review/preview-start", {"task": "T", "service": "web"}, "wrong")[0] == 403


@pytest.mark.skipif(
    not os.environ.get("AGENT_DEV_TEST_PLAYWRIGHT_MODULE") or not os.environ.get("AGENT_DEV_TEST_BROWSER_EXECUTABLE"),
    reason="Opt in with an installed Playwright module and browser; no auto install.",
)
def test_capture_comment_and_screen_decision_with_installed_browser(preview, monkeypatch):
    p = preview
    monkeypatch.setenv("DEVROSTER_PLAYWRIGHT_MODULE", os.environ["AGENT_DEV_TEST_PLAYWRIGHT_MODULE"])
    monkeypatch.setenv("DEVROSTER_BROWSER_EXECUTABLE", os.environ["AGENT_DEV_TEST_BROWSER_EXECUTABLE"])
    _, session = prepare_and_start(p, "launch")
    status, shot = call(p["server"], "/api/review/preview-capture", {"task": "T", "session_id": session["session_id"], "generation": session["generation"], "viewport": {"width": 800, "height": 600}}, p["web"].token)
    assert status == 200 and shot["available"] is True, shot
    elements = {e["testid"]: e for e in shot["elements"] if e["testid"]}
    assert {"title", "save"} <= set(elements)
    assert "hunter2-value" not in json.dumps(shot)
    assert any("external.invalid" in origin for origin in shot["blocked_origins"])
    status, image = call(p["server"], f"/api/review/preview-screenshot?task=T&capture={shot['id']}", raw=True)
    assert status == 200 and image.startswith(b"\x89PNG")
    save = elements["save"]["index"]
    status, thread = call(p["server"], "/api/review/preview-comment", {"task": "T", "capture_id": shot["id"], "element": save, "body": "버튼 문구 확인"}, p["web"].token)
    assert status == 200, thread
    decision = {"task": "T", "capture_id": shot["id"], "decision": "approved"}
    status, result = call(p["server"], "/api/review/preview-decision", decision, p["web"].token)
    assert status == 400 and "코멘트" in result["error"]
    call(p["server"], "/api/review/comment-state", {"task": "T", "thread_id": thread["thread_id"], "state": "resolved", "resolution": "확인", "expected_version": 0}, p["web"].token)
    status, result = call(p["server"], "/api/review/preview-decision", decision, p["web"].token)
    assert status == 200, result
    call(p["server"], "/api/review/preview-stop", {"task": "T", "session_id": session["session_id"]}, p["web"].token)
    wait_dead(pids(p))
    _, again = prepare_and_start(p, "launch")
    assert again["generation"] == session["generation"] + 1
    status, result = call(p["server"], "/api/review/preview-comment", {"task": "T", "capture_id": shot["id"], "element": save, "body": "old"}, p["web"].token)
    assert status == 409
    call(p["server"], "/api/review/preview-stop", {"task": "T", "session_id": again["session_id"]}, p["web"].token)


def test_setup_saves_validated_preview_services(tmp_path):
    from agent_dev.adapters.project_setup import ProjectSetup

    root = tmp_path / "project"
    value = ProjectSetup(root).read()
    value["create_directories"] = True
    module = value["modules"][0]["id"]
    value["preview_services"] = [{"id": "web", "module": module, "argv": ["npm", "run", "dev"], "port": 5173, "readiness_path": "/", "startup_timeout": 30}]
    ProjectSetup(root).save(value)
    _, _, profile = ProjectSetup(root).current_profile()
    assert profile["preview_services"][0]["host"] == "127.0.0.1"
    assert ProjectSetup(root).read()["preview_services"][0]["id"] == "web"
    bad = ProjectSetup(root).read()
    bad["preview_services"] = [{"id": "web", "module": module, "argv": ["npm"], "port": 5173, "host": "0.0.0.0"}]
    with pytest.raises(DomainError):
        ProjectSetup(root).save(bad)


def test_capture_from_older_source_is_not_current_and_cannot_be_approved(preview):
    from agent_dev.domain.content import content_digest, digest

    p = preview
    status, started = prepare_and_start(p, "launch")
    assert status == 200
    capture_dir = p["runtime"].store.db.path.parent / "preview" / "T"
    capture_dir.mkdir(parents=True, exist_ok=True)
    image = b"\x89PNG\r\n\x1a\nfake"
    manifest = {
        "id": "a" * 32,
        "session_id": started["session_id"],
        "generation": started["generation"],
        "service": "web",
        "path": "/",
        "final_url": "",
        "viewport": {"width": 800, "height": 600},
        "screen_digest": content_digest(image),
        "source_digest": "0" * 64,
        "elements": [{"index": 0, "testid": "save", "tag": "button", "role": "", "text": "Save", "path": "button", "rect": {"x": 0, "y": 0, "width": 10, "height": 10}}],
        "blocked_origins": [],
        "created_at": 1.0,
    }
    manifest["capture_digest"] = digest(manifest)
    (capture_dir / f"{manifest['id']}.png").write_bytes(image)
    (capture_dir / f"{manifest['id']}.json").write_text(json.dumps(manifest))
    _, data = call(p["server"], "/api/review/preview?task=T")
    assert data["captures"][0]["current"] is False
    status, _ = call(p["server"], "/api/review/preview-decision", {"task": "T", "capture_id": manifest["id"], "decision": "approved"}, p["web"].token)
    assert status == 409
    status, _ = call(p["server"], "/api/review/preview-comment", {"task": "T", "capture_id": manifest["id"], "element": 0, "body": "x"}, p["web"].token)
    assert status == 409
    (capture_dir / f"{manifest['id']}.png").write_bytes(image + b"tampered")
    status, _ = call(p["server"], f"/api/review/preview-screenshot?task=T&capture={manifest['id']}", raw=True)
    assert status == 400
    call(p["server"], "/api/review/preview-stop", {"task": "T", "session_id": started["session_id"]}, p["web"].token)


def test_restart_lost_session_is_read_as_ended_and_reconciled_on_start(preview):
    p = preview
    status, session = prepare_and_start(p, "launch")
    assert status == 200 and session["owned"] is True
    # Simulate a server restart: the in-memory ownership and the process are gone.
    preview_host._stop_owned(session["session_id"])
    assert wait_dead(pids(p))
    db = p["runtime"].store.db
    status, data = call(p["server"], "/api/review/preview?task=T")
    assert status == 200
    shown = next(s for s in data["sessions"] if s["id"] == session["session_id"])
    assert shown["state"] == "ended" and shown["process_lost"] is True
    stored = db.rows("SELECT state FROM preview_sessions WHERE id=?", (session["session_id"],))[0]["state"]
    assert stored == "active", "GET never writes"
    status, again = prepare_and_start(p, "launch")
    assert status == 200
    assert db.rows("SELECT state FROM preview_sessions WHERE id=?", (session["session_id"],))[0]["state"] == "stopped"
    call(p["server"], "/api/review/preview-stop", {"task": "T", "session_id": again["session_id"]}, p["web"].token)
