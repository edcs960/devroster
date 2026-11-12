"""The local screens answer only a browser this app opened (audit S-03, S-05).

``conftest.py`` gives every ordinary test request the session cookie. The requests here
go through ``ANONYMOUS``, which sends only what the test says: another program on the
computer, or a browser with exactly the cookie given.
"""

import html
import http.client
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_dev import app
from agent_dev.ui import session
from agent_dev.ui.local_server import LocalHTTPServer


class _Stranger(http.client.HTTPConnection):
    session_cookie = False  # conftest.py adds the cookie to every other local request.


class _StrangerHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_Stranger, req)


ANONYMOUS = urllib.request.build_opener(_StrangerHandler)


class Served:
    def __init__(self, instance):
        self.instance = instance
        self.port = instance.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=instance.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path, *, cookie=None, method="GET", headers=None):
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(
            self.base + path, method=method, headers=headers, data=b"" if method == "POST" else None
        )
        try:
            with ANONYMOUS.open(request, timeout=5) as response:
                return response.status, response.headers, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read().decode("utf-8")

    def close(self):
        self.instance.shutdown()
        studio = getattr(self.instance, "studio", None)
        if studio is not None:
            studio.close()
        self.instance.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def served(home):
    from agent_dev.ui.studio_server import make_server

    server = Served(make_server(home / "project", 0))
    yield server
    server.close()


def good_cookie(port="any"):
    return f"{session.COOKIE_PREFIX}{port}={session.SECRET}"


def refused(result):
    status, _headers, body = result
    return status == 403 and json.loads(body).get("code") == "session_required"


DATA = [
    "/api/studio/bootstrap",
    "/api/agent/bootstrap",
    "/api/setup/bootstrap",
    "/api/setup/bootstrap?root=/",
    "/api/setup/folders?path=/",
    "/api/settings/effective?root=/",
]


@pytest.mark.parametrize("path", DATA)
def test_another_program_gets_no_tokens_or_folder_lists(served, path):
    status, headers, body = served.get(path)
    assert refused((status, headers, body))
    assert "token" not in body.lower()
    assert headers["Cache-Control"] == "no-store"
    assert not refused(served.get(path, cookie=good_cookie()))


def test_the_browser_the_app_opened_reads_its_data(served):
    status, _headers, body = served.get("/api/studio/bootstrap", cookie=good_cookie())
    assert status == 200 and isinstance(json.loads(body), dict)


def test_quit_needs_the_session(served):
    stopped = threading.Event()
    served.instance.studio.quit = stopped.set
    assert refused(served.get("/api/app/quit", method="POST", headers={"X-DevRoster-Action": "quit"}))
    assert not stopped.is_set()


def test_writes_need_the_session_whatever_the_method(served):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        status, _headers, body = served.get("/api/setup/project", method=method)
        # The server has no PUT/PATCH/DELETE at all; what matters is that none reaches a route.
        assert status in {403, 501} and "token" not in body.lower()
    assert refused(served.get("/api/setup/project", method="POST"))


def test_a_wrong_cookie_is_no_session(served):
    path = "/api/studio/bootstrap"
    assert refused(served.get(path, cookie=f"{session.COOKIE_PREFIX}{served.port}=wrong"))
    assert refused(served.get(path, cookie=f"{session.COOKIE_PREFIX}{served.port}={session.SECRET}x"))
    assert refused(served.get(path, cookie=f"other={session.SECRET}"))
    assert refused(served.get(path, cookie="broken; ;=;"))
    assert refused(served.get(path, cookie=f"{session.COOKIE_PREFIX}{served.port}=\udcff".encode("utf-8", "replace").decode()))


def test_what_stays_open_without_a_session(served):
    status, _headers, body = served.get("/api/app/status")
    assert status == 200 and all(type(value) is bool for value in json.loads(body).values())
    status, headers, body = served.get("/ui-tokens.css")
    assert status == 200 and headers["Content-Type"].startswith("text/css")
    # The OAuth callback is protected by its own state value, not by the cookie.
    status, _headers, body = served.get("/api/integrations/callback?state=unknown&code=x")
    assert "session_required" not in body


def test_a_page_without_a_session_explains_what_to_do(served):
    for path in ("/", "/studio", "/setup", "/agent", "/settings", "/ai", "/unknown"):
        status, headers, body = served.get(path)
        assert status == 403, path
        assert headers["Content-Type"].startswith("text/html")
        assert "DevRoster 앱에서 다시 열어주세요" in body and "devroster app" in body
        assert "<script" not in body and "style=" not in body  # The pages' CSP allows neither.
        assert "default-src 'self'" in headers["Content-Security-Policy"]


def test_a_launch_key_starts_a_session_once(served):
    key = session.mint()
    status, headers, body = served.get(f"/launch?key={key}&next=%2Fstudio%3Ftab%3Dplan")
    assert status == 200
    cookie = headers["Set-Cookie"]
    name, _, rest = cookie.partition("=")
    value, *attributes = [part.strip() for part in rest.split(";")]
    assert name == session.cookie_name(served.port) and value == session.SECRET
    assert {"Path=/", "HttpOnly", "SameSite=Strict"} <= set(attributes)
    assert "Secure" not in attributes  # Plain http on 127.0.0.1.
    assert '<meta http-equiv="refresh" content="0;url=/studio?tab=plan">' in body
    assert key not in body and session.SECRET not in body
    assert served.get("/api/studio/bootstrap", cookie=f"{name}={value}")[0] == 200
    # The same address again (browser history, a copied link) gives nothing.
    status, headers, _body = served.get(f"/launch?key={key}&next=%2Fstudio")
    assert status == 403 and headers.get("Set-Cookie") is None


def test_a_used_launch_address_only_forwards_a_browser_that_has_the_session(served):
    status, headers, body = served.get("/launch?key=gone&next=%2Fagent", cookie=good_cookie(served.port))
    assert status == 200 and headers.get("Set-Cookie") is None and "url=/agent" in body


@pytest.mark.parametrize(
    "target",
    ["//evil.test/", "https://evil.test/", "/\\evil.test", "javascript:alert(1)", "/a b", '/"><meta', "", "studio"],
)
def test_launch_never_forwards_outside_this_server(served, target):
    assert session.safe_next(target) == "/"
    key = session.mint()
    _status, _headers, body = served.get("/launch?" + urllib.parse.urlencode({"key": key, "next": target}))
    assert '<meta http-equiv="refresh" content="0;url=/">' in body
    assert "evil.test" not in body


def test_launch_through_another_host_name_is_refused(served):
    key = session.mint()
    status, headers, _body = served.get(f"/launch?key={key}", headers={"Host": f"evil.test:{served.port}"})
    assert status == 403 and headers.get("Set-Cookie") is None
    assert session.redeem(key)  # Not used up by the refused request.


def test_launch_rejects_keys_nobody_issued(served):
    for key in ("", "x", "a" * 500, session.SECRET):
        status, headers, _body = served.get("/launch?" + urllib.parse.urlencode({"key": key}))
        assert status == 403 and headers.get("Set-Cookie") is None
    assert served.get("/launch")[0] == 403
    assert served.get("/launch?" + "&".join(f"k{i}=1" for i in range(20)))[0] == 403


def test_a_revoked_key_is_refused():
    key = session.mint()
    session.revoke(key)
    assert not session.redeem(key)


def test_a_ticket_reaches_a_running_app_once(home):
    key = session.ticket()
    folder = home / "home" / session.LAUNCH_DIR
    [file] = list(folder.iterdir())
    assert key not in file.name and key not in file.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert session.redeem(key)
    assert not file.exists()
    assert not session.redeem(key)


def test_an_expired_ticket_is_refused(home, monkeypatch):
    key = session.ticket()
    real = time.time
    monkeypatch.setattr(session.time, "time", lambda: real() + session.TICKET_SECONDS + 1)
    assert not session.redeem(key)
    assert list((home / "home" / session.LAUNCH_DIR).iterdir()) == []


def test_a_damaged_ticket_file_is_refused(home):
    key = session.ticket()
    [file] = list((home / "home" / session.LAUNCH_DIR).iterdir())
    file.write_text("not a time", encoding="utf-8")
    assert not session.redeem(key)


def test_purge_removes_only_old_launch_files(home):
    session.ticket()
    folder = home / "home" / session.LAUNCH_DIR
    [fresh] = list(folder.iterdir())
    old = folder / "open-1-old.html"
    old.write_text("x", encoding="utf-8")
    past = time.time() - session.TICKET_SECONDS - 5
    os.utime(old, (past, past))
    session.purge()
    assert not old.exists() and fresh.exists()


def launch_files(home):
    return sorted((home / "home" / session.LAUNCH_DIR).glob("open-*.html"))


def test_the_browser_is_given_a_private_file_not_the_key(home, monkeypatch):
    monkeypatch.setattr(session, "_wsl", lambda: False)
    monkeypatch.setattr(session, "OPENER_SECONDS", 0.3)
    opened = []
    assert session.open_in_browser("http://127.0.0.1:9", "/agent", opener=lambda target: opened.append(target) or True)
    [target] = opened
    assert target.startswith("file:")
    [file] = launch_files(home)
    assert file.as_uri() == target
    text = file.read_text(encoding="utf-8")
    found = re.search(r'url=(http://127\.0\.0\.1:9/launch\?key=([^&"]+)&amp;next=%2Fagent)"', text)
    assert found, text
    key = found.group(2)
    assert key not in target
    if os.name != "nt":
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    # Nobody opened it: the file goes away and the key stops working.
    deadline = time.time() + 5
    while file.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not file.exists()
    assert not session.redeem(key)
    assert opened == [target]  # A key that was withdrawn is not opened a second way.


def test_using_the_key_removes_the_file_that_carried_it(served, home, monkeypatch):
    monkeypatch.setattr(session, "_wsl", lambda: False)
    opened = []
    session.open_in_browser(served.base, "/setup", opener=lambda target: opened.append(target) or True)
    [file] = launch_files(home)
    address = html.unescape(re.search(r'url=([^"]+)"', file.read_text(encoding="utf-8")).group(1))
    assert served.get(address[len(served.base) :])[0] == 200
    assert not file.exists()
    time.sleep(0.3)
    assert len(opened) == 1  # The browser arrived: nothing else is opened.


def test_a_browser_that_cannot_read_the_file_gets_the_address(home, monkeypatch):
    """A sandboxed browser package, or ``.html`` files opened by an editor."""
    monkeypatch.setattr(session, "_wsl", lambda: False)
    monkeypatch.setattr(session, "FALLBACK_SECONDS", 0.2)
    opened = []
    key = session.ticket()
    session.open_in_browser("http://127.0.0.1:9", "/agent", key=key, opener=lambda target: opened.append(target) or True, wait=True)
    assert opened[0].startswith("file:")
    assert opened[1] == f"http://127.0.0.1:9/launch?key={key}&next=%2Fagent"
    assert session.redeem(key)  # Still usable by the browser that now got the address.


def test_wsl_gets_the_address_itself(home, monkeypatch):
    # A Windows browser cannot read a file in the Linux home folder.
    monkeypatch.setattr(session, "_wsl", lambda: True)
    monkeypatch.setattr(session, "OPENER_SECONDS", 0.05)
    opened = []
    session.open_in_browser("http://127.0.0.1:9", "/agent", opener=lambda target: opened.append(target) or True)
    assert opened[0].startswith("http://127.0.0.1:9/launch?key=") and launch_files(home) == []


def test_a_missing_browser_does_not_stop_the_app(home, monkeypatch):
    monkeypatch.setattr(session, "OPENER_SECONDS", 0.05)

    def broken(_target):
        raise RuntimeError("no browser")

    assert session.open_in_browser("http://127.0.0.1:9", "/", opener=broken) is False


def test_a_launch_address_from_the_terminal_works_in_a_browser(served):
    address = session.launch_url(served.base, "/setup")
    assert address.startswith(served.base + "/launch?key=") and address.endswith("&next=%2Fsetup")
    status, headers, _body = served.get(address[len(served.base) :])
    assert status == 200 and headers["Set-Cookie"].startswith(session.cookie_name(served.port) + "=")


def test_a_server_for_another_project_accepts_the_same_browser(served, home):
    """Cookies are not separated by port: the cookie one server set reaches the others."""
    from agent_dev.ui.studio_server import make_server

    other = Served(make_server(home / "second", 0))
    try:
        assert other.get("/api/studio/bootstrap", cookie=good_cookie(served.port))[0] == 200
        assert refused(other.get("/api/studio/bootstrap"))
    finally:
        other.close()


class _Web:
    page = "page"

    @staticmethod
    def handle(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"ok")


def _servers(root):
    from agent_dev.ui import dashboard, local_web, setup_server, studio_server

    yield "studio", studio_server.make_server(root / "a", 0)
    yield "setup", setup_server.make_server(root / "b", 0)
    yield "dashboard", dashboard.make_server(root / "c", 0)
    yield "local_web", local_web.make_server(_Web(), 0)


def test_every_server_of_the_product_is_guarded(home):
    for name, instance in _servers(home):
        server = Served(instance)
        try:
            assert isinstance(instance, LocalHTTPServer), name
            assert refused(server.get("/api/tasks")), name
            assert refused(server.get("/api/anything", method="POST")), name
            assert server.get("/page")[0] == 403, name
            assert server.get("/api/app/status")[0] != 403 or not refused(server.get("/api/app/status")), name
        finally:
            server.close()


def test_no_server_is_built_around_the_guard():
    """Every HTTP server in the product is a LocalHTTPServer, which applies the guard."""
    package = Path(session.__file__).resolve().parents[1]
    builders = []
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\b(?:Threading)?HTTPServer\(|\bTCPServer\(|socketserver\.\w+Server\(", text):
            builders.append(path.relative_to(package).as_posix())
    assert builders == []
    assert issubclass(LocalHTTPServer, HTTPServer)


def test_the_guard_keeps_the_handler_and_covers_every_method():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            pass

        def do_POST(self):
            pass

        do_PUT = do_POST

    guarded = session.guarded(Handler)
    assert issubclass(guarded, Handler) and guarded.__name__ == "Handler"
    for name in ("do_GET", "do_POST", "do_PUT"):
        assert getattr(guarded, name) is not getattr(Handler, name)


def test_a_second_launch_hands_the_running_app_a_ticket(served, home, monkeypatch, capsys):
    monkeypatch.setattr(app, "running_instance", lambda: served.base)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    app.main(["--no-browser"])
    printed = capsys.readouterr().out
    address = re.search(r"직접 열기\(한 번만, 10분 안에\): (\S+)", printed).group(1)
    assert address.startswith(served.base + "/launch?key=")
    status, headers, _body = served.get(address[len(served.base) :])
    assert status == 200 and headers["Set-Cookie"]
    assert served.get(address[len(served.base) :])[0] == 403


def test_a_second_launch_opens_the_browser_without_printing_a_key(served, home, monkeypatch, capsys):
    monkeypatch.setattr(app, "running_instance", lambda: served.base)
    monkeypatch.setattr(session, "_wsl", lambda: False)
    opened = []

    def browser(target):
        """A browser that reads the private file and follows it, as the real one does."""
        opened.append(target)
        [file] = launch_files(home)
        assert file.as_uri() == target
        address = html.unescape(re.search(r'url=([^"]+)"', file.read_text(encoding="utf-8")).group(1))
        status, headers, _body = served.get(address[len(served.base) :])
        assert status == 200 and headers["Set-Cookie"]
        return True

    monkeypatch.setattr(session.webbrowser, "open", browser)
    started = time.monotonic()
    app.main([])
    assert time.monotonic() - started < session.FALLBACK_SECONDS  # Returned when the browser arrived.
    assert "key=" not in capsys.readouterr().out
    assert len(opened) == 1 and launch_files(home) == []


def test_a_static_looking_page_address_is_still_a_page(served):
    # ``urlparse`` (used by the routes) cuts ";..." off the path, so this is /studio.
    assert served.get("/studio;x.css")[0] == 403
    assert served.get("/studio;x.css", cookie=good_cookie())[0] == 200
    assert served.get("//[")[0] == 403  # Not an address at all.


def test_the_packaged_app_prints_no_key(served, home, monkeypatch, capsys):
    monkeypatch.setattr(app, "running_instance", lambda: served.base)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    app.main(["--no-browser"])
    assert "key=" not in capsys.readouterr().out


def test_a_real_browser_follows_the_private_file_into_a_session(home, monkeypatch):
    import shutil
    import subprocess

    from agent_dev.ui.studio_server import make_server

    module = os.environ.get("AGENT_DEV_TEST_PLAYWRIGHT_MODULE")
    executable = os.environ.get("AGENT_DEV_TEST_BROWSER_EXECUTABLE")
    if not shutil.which("node") or not module or not executable:
        pytest.skip("Opt in with an installed Playwright module and browser executable; no auto install.")
    if not Path(module).exists() or not Path(executable).is_file():
        pytest.skip("Configured optional browser or Playwright module is unavailable.")
    monkeypatch.setattr(session, "_wsl", lambda: False)
    opened = []
    first, second = Served(make_server(home / "one", 0)), Served(make_server(home / "two", 0))
    try:
        session.open_in_browser(first.base, "/setup", opener=lambda target: opened.append(target) or True)
        # This browser must earn its cookie: no test cookie from the preload file.
        environment = {key: value for key, value in os.environ.items() if key != "DEVROSTER_TEST_SESSION"}
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("web_session_browser.js")), first.base, opened[0], second.base, module, executable],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=environment,
        )
        assert result.returncode == 0, result.stderr
    finally:
        first.close()
        second.close()
