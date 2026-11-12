"""One browser session for every local screen of this process (audit S-03, S-05).

The screens listen on 127.0.0.1, but any program or any other user on the computer can
connect to a loopback port. Checking ``Host`` and ``Origin`` stops other web sites; it
does not stop a local program that simply sends the right headers. So a request is served
only when it carries this process's session cookie, and the cookie is handed out only
through a one-time launch address that the app itself opens (or prints in its terminal).

- ``guarded`` wraps a handler class so that every request passes ``admit`` first.
  ``LocalHTTPServer`` applies it, which covers every server of the product.
- ``/launch?key=<one-time key>&next=<page>`` sets the cookie and moves on to the page.
- Without a session only static files (no data), ``/api/app/status`` (three booleans a
  second ``devroster app`` needs to find the running one) and the OAuth callback
  (protected by its own ``state``) are served.

The secret lives in memory and changes with every start. The cookie name carries the
port because browsers do not separate cookies by port: two DevRoster processes on one
computer would otherwise overwrite each other's cookie. For the same reason a server
this process opens for another project (``workspace_sessions``) accepts the cookie the
first one set.

Known limit: because cookies ignore the port, another local user who runs a listener
on 127.0.0.1 and gets this user to open it in the same browser receives the cookie.
"""

from __future__ import annotations

import atexit
import functools
import hashlib
import hmac
import html
import json
import os
import platform
import re
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

SECRET = secrets.token_urlsafe(32)
COOKIE_PREFIX = "devroster_session_"
COOKIE_SECONDS = 30 * 24 * 3600  # Useless anyway once this process (and its secret) ends.
STATIC_SUFFIXES = (".css", ".js", ".svg", ".ico", ".png", ".woff2")
OPEN_GETS = frozenset({"/api/app/status", "/api/integrations/callback"})
TICKET_SECONDS = 600
OPENER_SECONDS = 120
FALLBACK_SECONDS = 8
LAUNCH_DIR = "launch"
_NEXT = re.compile(r"/(?!/)[A-Za-z0-9._~/?&=%+,:@-]*\Z")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", _CSP),
)

_lock = threading.Lock()
_keys: set[str] = set()


# --- keys ---------------------------------------------------------------------------


def mint() -> str:
    """A one-time key this process accepts at ``/launch``."""
    key = secrets.token_urlsafe(32)
    with _lock:
        _keys.add(key)
    return key


def revoke(key: str) -> None:
    with _lock:
        _keys.discard(key)


def _launch_dir() -> Path:
    from agent_dev.core.cli_home import app_home

    path = app_home() / LAUNCH_DIR
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _private_file(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _ticket_path(key: str) -> Path:
    return _launch_dir() / _digest(key)


def _opener_path(key: str) -> Path:
    return _launch_dir() / f"open-{_digest(key)}.html"


def ticket() -> str:
    """A one-time key for a DevRoster that is already running (another process).

    Kept as a private file in the app folder both processes share; the name is the
    key's hash, so the key itself is never stored.
    """
    key = secrets.token_urlsafe(32)
    _private_file(_ticket_path(key), str(time.time() + TICKET_SECONDS))
    return key


def redeem(key: str) -> bool:
    """Use up a key. True exactly once per key."""
    if not key or len(key) > 200:
        return False
    try:
        _opener_path(key).unlink(missing_ok=True)  # The file that brought the browser here.
    except OSError:
        pass
    with _lock:
        if key in _keys:
            _keys.discard(key)
            return True
    try:
        path = _ticket_path(key)
        expires = float(path.read_text(encoding="utf-8"))
        path.unlink()  # Whoever removes the file is the one that used the ticket.
    except (OSError, ValueError):
        return False
    return time.time() < expires


def pending(key: str) -> bool:
    """Whether nobody has used this key yet."""
    with _lock:
        if key in _keys:
            return True
    try:
        return _ticket_path(key).is_file()
    except OSError:
        return False


def purge(now: float | None = None) -> None:
    """Remove launch files nobody used (a browser that never opened, an expired ticket)."""
    now = time.time() if now is None else now
    try:
        entries = list(_launch_dir().iterdir())
    except OSError:
        return
    for path in entries:
        try:
            if now - path.stat().st_mtime > TICKET_SECONDS:
                path.unlink()
        except OSError:
            pass


# --- addresses ----------------------------------------------------------------------


def safe_next(value: str) -> str:
    """A page of this server to continue to; anything else becomes the start page."""
    return value if isinstance(value, str) and len(value) <= 2000 and _NEXT.match(value) else "/"


def launch_url(base: str, path: str = "/", key: str | None = None) -> str:
    """The address that starts a browser session and continues to ``path``."""
    return f"{base}/launch?key={key or mint()}&next={quote(safe_next(path), safe='')}"


def _wsl() -> bool:
    return "microsoft" in platform.uname().release.lower()


def _try(opener, target: str) -> bool:
    try:
        return bool(opener(target))
    except Exception:  # noqa: BLE001 - a missing browser must not stop the app
        return False


def open_in_browser(base: str, path: str = "/", *, key: str | None = None, opener=None, wait: bool = False) -> bool:
    """Open ``path`` of the server at ``base`` in the user's browser with a session.

    The launch address is not passed on a command line, where other users of the
    computer could read it from the process list: the browser is given a private file
    that forwards to it. The server removes the file when the key is used; an unused
    file and key are removed shortly after.

    Some browsers cannot read that file (a sandboxed browser package, ``.html`` files
    opened by an editor). When nobody used the key after a few seconds the address
    itself is opened. ``wait`` does that check before returning, for a process that
    ends right after this call.
    """
    opener = opener or webbrowser.open
    minted = key is None
    key = key or mint()
    url = launch_url(base, path, key)
    target = url
    file: Path | None = None
    if not _wsl():  # A Windows browser cannot read a file inside the Linux home.
        try:
            file = _opener_path(key)
            link = html.escape(url, quote=True)
            _private_file(
                file,
                '<!doctype html><html lang="ko"><meta charset="utf-8">'
                f'<meta http-equiv="refresh" content="0;url={link}"><title>DevRoster</title>'
                f'<p><a href="{link}">DevRoster 열기</a></p></html>\n',
            )
            target = file.as_uri()
        except OSError:
            file = None

    def clean():
        if file is not None:
            try:
                file.unlink(missing_ok=True)
            except OSError:
                pass
        if minted:
            revoke(key)

    def follow():
        deadline = time.monotonic() + FALLBACK_SECONDS
        while time.monotonic() < deadline:
            if not pending(key):
                return
            time.sleep(0.1)
        _try(opener, url)

    if minted:  # This process stays: tidy up later, and at the latest when it ends.
        timer = threading.Timer(OPENER_SECONDS, clean)
        timer.daemon = True
        timer.start()
        atexit.register(clean)
    opened = _try(opener, target)
    if file is None:
        return opened
    if not opened:  # Nothing on this computer opens the file.
        return _try(opener, url)
    if wait:
        follow()
    else:
        threading.Thread(target=follow, daemon=True).start()
    return True


# --- requests -----------------------------------------------------------------------


def cookie_name(port: int) -> str:
    return f"{COOKIE_PREFIX}{port}"


def has_session(handler) -> bool:
    for header in handler.headers.get_all("Cookie") or []:
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name.startswith(COOKIE_PREFIX) and hmac.compare_digest(
                value.encode("utf-8", "replace"), SECRET.encode("utf-8")
            ):
                return True
    return False


def _send(handler, status: int, body: bytes, kind: str, extra=()) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", kind + "; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    for name, value in (*_HEADERS, *extra):
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def _page(title: str, lines: list[str], refresh: str | None = None) -> bytes:
    head = f'<meta http-equiv="refresh" content="0;url={html.escape(refresh, quote=True)}">' if refresh else ""
    body = "".join(f"<p>{line}</p>" for line in lines)
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"{head}<title>DevRoster</title>"
        '<link rel="stylesheet" href="/ui-tokens.css"><link rel="stylesheet" href="/ui-base.css">'
        f"</head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>\n"
    ).encode()


_WHY = (
    "이 화면은 DevRoster 앱이 직접 연 브라우저에서만 사용할 수 있습니다. "
    "앱을 다시 시작했거나, 주소를 직접 입력했거나, 다른 브라우저에서 열면 이 안내가 나옵니다."
)
_REOPEN = (
    "DevRoster 앱을 한 번 더 실행하거나 터미널에서 <code>devroster app</code>을 다시 실행하면 "
    "이미 실행 중인 화면이 이 브라우저에 열립니다."
)
_BY_HAND = "터미널에서 실행했다면 시작할 때 출력된 '직접 열기' 주소로도 열 수 있습니다. 그 주소는 한 번만 쓸 수 있습니다."


def _no_session(handler) -> None:
    _send(handler, 403, _page("DevRoster 앱에서 다시 열어주세요", [_WHY, _REOPEN, _BY_HAND]), "text/html")


def _launch(handler, query: str) -> None:
    port = handler.server.server_address[1]
    host = handler.headers.get("Host", "")
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
        return _no_session(handler)
    try:
        values = parse_qs(query, keep_blank_values=True, max_num_fields=4)
    except ValueError:
        return _no_session(handler)
    target = safe_next(values.get("next", ["/"])[0])
    cookies = ()
    if redeem(values.get("key", [""])[0]):
        cookies = (
            (
                "Set-Cookie",
                f"{cookie_name(port)}={SECRET}; Path=/; Max-Age={COOKIE_SECONDS}; HttpOnly; SameSite=Strict",
            ),
        )
    elif not has_session(handler):
        return _no_session(handler)
    # A page that forwards, not a redirect: the cookie is SameSite=Strict, and a browser
    # does not send it on a redirect chain that another site (or a local file) started.
    link = html.escape(target, quote=True)
    _send(
        handler,
        200,
        _page("DevRoster를 여는 중입니다", [f'<a href="{link}">화면이 바뀌지 않으면 여기를 눌러주세요.</a>'], refresh=target),
        "text/html",
        cookies,
    )


def admit(handler) -> bool:
    """Whether the wrapped handler may serve this request. Answers it here when not."""
    try:
        parsed = urlsplit(handler.path)
        path, query = parsed.path, parsed.query
    except ValueError:  # Not an address any route would serve.
        path, query = "/api/", ""
    get = handler.command == "GET"
    if get and path == "/launch":
        _launch(handler, query)
        return False
    if has_session(handler):
        return True
    api = path.startswith("/api/")
    # ``;`` starts path parameters for ``urlparse``, which some routes use: "/page;x.css"
    # is the page there, not a stylesheet.
    if get and (path in OPEN_GETS or not api and ";" not in path and path.endswith(STATIC_SUFFIXES)):
        return True
    if get and not api:
        _no_session(handler)
        return False
    _send(
        handler,
        403,
        json.dumps(
            {"error": "DevRoster 앱에서 화면을 다시 열어주세요.", "code": "session_required"},
            ensure_ascii=False,
        ).encode("utf-8"),
        "application/json",
    )
    return False


def _checked(method):
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        if admit(self):
            return method(self, *args, **kwargs)
        return None

    return run


def guarded(handler_class):
    """The same handler class, with every request method behind ``admit``."""
    methods = {
        name: _checked(getattr(handler_class, name))
        for name in dir(handler_class)
        if name.startswith("do_") and callable(getattr(handler_class, name))
    }
    return type(handler_class.__name__, (handler_class,), methods)
