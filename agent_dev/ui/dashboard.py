"""A portable, loopback-only, read-only workflow dashboard."""

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.adapters.workspace import configuration_root, metadata
from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError, ErrorCode


def read_view(root, parsed):
    """Shared read-only API used by both local web entry points."""
    query = TaskQueries(root)
    if parsed.path == "/api/tasks":
        params = parse_qs(parsed.query)
        allowed = {"limit", "page", "search", "state", "project"}
        if set(params) - allowed or any(len(values) != 1 for values in params.values()):
            raise ValueError("Invalid filters")
        result = query.tasks(
            int(params.get("limit", ["100"])[0]),
            page=int(params.get("page", ["1"])[0]),
            search=params.get("search", [""])[0],
            state=params.get("state", [""])[0],
            project=params.get("project", [""])[0],
        )
        result["project"] = redact(metadata(root) or {"name": query.workspace.name}, query.secrets)
        return result
    if parsed.path == "/api/task":
        task_id = parse_qs(parsed.query).get("id", [""])[0]
        if not task_id or len(task_id) > 200:
            raise ValueError("Select a valid task ID.")
        from agent_dev.ui.presentation import task_display

        result = query.explain(task_id)
        result["display"] = task_display(result)
        return result
    if parsed.path == "/api/doctor":
        from agent_dev.adapters.diagnostics import diagnose

        return diagnose(root)
    raise ValueError("Unknown view.")


def make_server(root: Path, port=8766):
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid dashboard port.")
    root = configuration_root(root)
    from agent_dev.ui.studio_server import StudioWeb

    setup = StudioWeb(root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def local_request(self):
            port = cast(tuple[str, int], self.server.server_address)[1]
            allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            return (
                host in allowed
                and (not origin or origin == f"http://{host}")
                and self.headers.get("Sec-Fetch-Site", "same-origin") in {"none", "same-origin"}
            )

        def response(self, status, payload, content_type="application/json; charset=utf-8"):
            body = (
                payload
                if isinstance(payload, bytes)
                else json.dumps(payload, ensure_ascii=False).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.local_request():
                return self.response(403, {"error": "Local same-origin requests only."})
            parsed = urlsplit(self.path)
            if setup.matches(parsed.path):
                return setup.handle(self)
            if parsed.path == "/favicon.ico":
                return self.response(204, b"", "image/x-icon")
            assets = {
                "/": ("dashboard.html", "text/html"),
                "/dashboard": ("dashboard.html", "text/html"),
                "/dashboard.js": ("dashboard.js", "text/javascript"),
                "/dashboard.css": ("dashboard.css", "text/css"),
            }
            if parsed.path in assets:
                name, content_type = assets[parsed.path]
                return self.response(
                    200,
                    files("agent_dev.ui").joinpath("assets", name).read_bytes(),
                    content_type + "; charset=utf-8",
                )
            try:
                if parsed.path not in {"/api/tasks", "/api/task", "/api/doctor"}:
                    return self.response(404, {"error": "Unknown view."})
                return self.response(200, read_view(root, parsed))
            except (DomainError, OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                return self.response(
                    400,
                    {
                        "error": "작업 정보를 불러오지 못했습니다. 작업을 다시 선택하거나 환경 진단을 확인하세요."
                    },
                )

        def do_POST(self):
            if self.command == "POST" and setup.matches(urlsplit(self.path).path):
                return setup.handle(self)
            self.response(405, {"error": "Dashboard is read-only; use approved CLI commands."})

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(root, port=8766):
    server = make_server(root, port)
    print(f"Agent Dev dashboard: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {"stopped": True}
