"""Loopback-only planning studio, standalone or mounted in the existing web server."""

import hmac
import json
import secrets
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_dev.adapters.ai_providers import metadata as provider_metadata
from agent_dev.adapters.studio_generation import StudioJobs
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.application.studio import Studio
from agent_dev.domain.errors import DomainError
from agent_dev.domain.studio import DOCUMENTS, StudioError
from agent_dev.ui.page_navigation import local_page_navigation
from agent_dev.ui.shared_assets import shared_asset_name

ASSETS = {
    "/studio": ("studio.html", "text/html"),
    "/studio/": ("studio.html", "text/html"),
    "/studio.js": ("studio.js", "text/javascript"),
    "/studio.css": ("studio.css", "text/css"),
    "/mock.css": ("mock.css", "text/css"),
    "/mock_renderer.js": ("mock_renderer.js", "text/javascript"),
}


class StudioWeb:
    def __init__(self, root, generator=None):
        self.root = Path(root).expanduser().resolve()
        self.assets = files("agent_dev.ui").joinpath("assets")
        self.workspace_sessions = None
        self._studio = None
        self._jobs = None
        self._generator = generator
        self._initialization_lock = threading.RLock()
        from agent_dev.ui.setup_server import SetupWeb

        self.setup = SetupWeb(root)
        self.token = secrets.token_urlsafe(32)
        from agent_dev.ui.code_stamp import code_stamp

        self.code_stamp = code_stamp()

    @property
    def studio(self) -> Studio:
        # Static GETs must work before a workspace or Studio database exists.
        with self._initialization_lock:
            if self._studio is None:
                self._studio = Studio(StudioStore(self.root), self.assets)
            return self._studio

    @property
    def jobs(self) -> StudioJobs:
        with self._initialization_lock:
            if self._jobs is None:
                self._jobs = (
                    StudioJobs(self.studio, config_root=self.root)
                    if self._generator is None
                    else StudioJobs(self.studio, self._generator, config_root=self.root)
                )
            return self._jobs

    def close(self):
        if self.workspace_sessions is not None:
            self.workspace_sessions.close()
        if self._jobs is not None:
            self._jobs.close()
        self.setup.close()

    @staticmethod
    def matches(path):
        from agent_dev.ui.setup_server import SetupWeb

        return (
            shared_asset_name(path) is not None
            or path in ASSETS
            or path.startswith("/api/studio/")
            or SetupWeb.matches(path)
        )

    @staticmethod
    def send(handler, data, kind="application/json", status=200, filename=None):
        if kind == "application/json":
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header(
            "Content-Type", kind + ("; charset=utf-8" if kind != "application/zip" else "")
        )
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        if filename:
            handler.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        handler.end_headers()
        handler.wfile.write(data)

    def handle(self, handler):
        if self.setup.matches(urlparse(handler.path).path):
            self.setup.handle(handler)
            return
        try:
            parsed = urlparse(handler.path)
            host = handler.headers.get("Host", "")
            port = handler.server.server_address[1]
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                raise StudioError("로컬 주소로 접속해주세요.", 403)
            origin = handler.headers.get("Origin")
            if origin and origin != "http://" + host:
                raise StudioError("다른 사이트에서 접근할 수 없습니다.", 403)
            if (handler.headers.get("Sec-Fetch-Site", "") not in {"", "none", "same-origin"}
                and not local_page_navigation(handler, parsed.path, {"/studio", "/studio/"})):
                raise StudioError("같은 사이트에서 접근해주세요.", 403)
            if handler.command == "GET":
                if parsed.path == "/favicon.ico":
                    self.send(handler, b"", "image/x-icon", status=204)
                    return
                if parsed.path in ASSETS:
                    name, kind = ASSETS[parsed.path]
                    self.send(handler, self.assets.joinpath(name).read_bytes(), kind)
                    return
                if parsed.path in {"/dashboard", "/dashboard/"}:
                    # Status and execution share the agent workroom in the app; the standalone
                    # `devroster dashboard` command still serves its own read-only page.
                    target = "/agent" + ("?" + parsed.query if parsed.query else "")
                    handler.send_response(302)
                    handler.send_header("Location", target)
                    handler.send_header("Content-Length", "0")
                    handler.send_header("Cache-Control", "no-store")
                    handler.end_headers()
                    return
                dashboard_assets = {
                    "/dashboard.js": ("dashboard.js", "text/javascript"),
                    "/dashboard.css": ("dashboard.css", "text/css"),
                }
                if parsed.path in dashboard_assets:
                    name, kind = dashboard_assets[parsed.path]
                    self.send(handler, self.assets.joinpath(name).read_bytes(), kind)
                    return
                if parsed.path == "/api/app/status":
                    from agent_dev.ui.code_stamp import code_stamp

                    self.send(handler, {"restart_required": code_stamp() != self.code_stamp})
                    return
                if parsed.path in {"/api/tasks", "/api/task", "/api/doctor"}:
                    from agent_dev.ui.dashboard import read_view

                    self.send(handler, read_view(self.root, parsed))
                    return
                self.studio.recover_jobs()
                query = parse_qs(parsed.query)
                project_id = query.get("id", [""])[0]
                if parsed.path == "/api/studio/bootstrap":
                    from agent_dev.adapters.preparation import is_preparation
                    from agent_dev.adapters.project_setup import ai_defaults, ai_tool_notes

                    template = (
                        files("agent_dev.templates")
                        .joinpath("planning-brief.md")
                        .read_text(encoding="utf-8")
                    )
                    self.send(
                        handler,
                        {
                            "token": self.token,
                            "template": template,
                            "documents": DOCUMENTS,
                            "projects": self.studio.store.projects(),
                            "ai": ai_defaults(self.root),
                            "ai_notes": ai_tool_notes(self.root),
                            "preparation_mode": is_preparation(self.root),
                            "providers": provider_metadata(),
                        },
                    )
                elif parsed.path == "/api/studio/project":
                    revision = query.get("revision", [None])[0]
                    spec_revision = query.get("spec_revision", [None])[0]
                    self.send(
                        handler,
                        self.studio.store.detail(
                            project_id,
                            int(revision) if revision else None,
                            int(spec_revision) if spec_revision else None,
                        ),
                    )
                elif parsed.path in {"/api/studio/specs.zip", "/api/studio/handoff.zip"}:
                    final = parsed.path.endswith("handoff.zip")
                    self.send(
                        handler,
                        self.studio.bundle(project_id, final),
                        "application/zip",
                        filename="development-handoff.zip" if final else "specifications.zip",
                    )
                else:
                    raise StudioError("요청한 페이지를 찾을 수 없습니다.", 404)
                return
            if handler.command != "POST":
                raise StudioError("지원하지 않는 요청 방식입니다.", 405)
            if not hmac.compare_digest(handler.headers.get("X-Studio-Token", ""), self.token):
                raise StudioError("화면을 새로고침한 뒤 다시 시도해주세요.", 403)
            if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise StudioError("JSON 요청이 필요합니다.", 415)
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                raise StudioError("입력 크기를 1MB 이하로 줄여주세요.", 413)
            payload = json.loads(handler.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise StudioError("입력 형식이 잘못되었습니다.")
            result = self.mutate(parsed.path.rsplit("/", 1)[-1], payload)
            self.send(handler, result)
        except DomainError as exc:
            from agent_dev.adapters.preparation import PreparationModeError

            if isinstance(exc, PreparationModeError):
                self.send(handler, {"error": str(exc), "code": exc.code.value}, status=403)
                return
            self.send(
                handler,
                {"error": "작업 정보를 불러오지 못했습니다. 개발 환경 진단을 확인해주세요."},
                status=400,
            )
        except StudioError as exc:
            self.send(handler, {"error": str(exc)}, status=exc.status)
        except (ValueError, KeyError, TypeError, RecursionError):
            self.send(handler, {"error": "입력 형식을 확인해주세요."}, status=400)
        except (OSError, sqlite3.Error):
            self.send(
                handler,
                {"error": "저장소를 사용할 수 없습니다. 로컬 환경을 확인해주세요."},
                status=503,
            )

    def mutate(self, action, p):
        service = self.studio
        if action == "create":
            return service.create(p["title"], p["brief"])
        project, version = p["id"], p["version"]
        if type(version) is not int:
            raise StudioError("저장 버전이 필요합니다.")
        if action == "mock-carry":
            return service.carry_mock(project, version)
        if action == "brief":
            return service.save_brief(project, version, p["brief"], p["title"])
        if action == "spec-save":
            return service.save_specs(project, version, p["payload"])
        if action == "spec-approve":
            return service.approve_specs(project, version)
        if action == "generate":
            return self.jobs.start(
                project, version, p["kind"], p.get("provider"), p.get("model"), p.get("effort")
            )
        if action == "cancel":
            return service.cancel_job(project, version, p["job_id"])
        revision = p["revision"]
        if action == "mock-save":
            return service.save_mock(project, version, revision, p.get("payload"))
        if action == "comment":
            return service.add_comment(
                project, version, revision, p["screen"], p["element"], p["body"]
            )
        if action == "comment-state":
            return service.process_comment(
                project, version, revision, p["comment_id"], p["state"], p.get("resolution", "")
            )
        if action == "screen-approve":
            return service.approve_screen(
                project, version, revision, p["screen"], p.get("note", "")
            )
        raise StudioError("요청한 기능을 찾을 수 없습니다.", 404)


class StudioHTTPServer(ThreadingHTTPServer):
    studio: StudioWeb


def make_server(root, port=8767, generator=None):
    web = StudioWeb(root, generator)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                from agent_dev.app import start_path

                # Same entry as the app launcher: setup until the project is saved.
                self.send_response(302)
                self.send_header("Location", start_path(web.root))
                self.end_headers()
                return
            web.handle(self)

        def do_POST(self):
            web.handle(self)

        def log_message(self, format, *args):
            pass

    try:
        server = StudioHTTPServer(("127.0.0.1", port), Handler)
    except BaseException:
        web.close()
        raise
    server.studio = web
    from agent_dev.ui.workspace_sessions import WorkspaceSessions
    WorkspaceSessions().bind(server, owner=True)
    return server


def serve(root, port=8767):
    server = make_server(root, port)
    print(f"기획·화면 검토 Studio: http://127.0.0.1:{server.server_port}/studio")
    print("종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.studio.close()
        server.server_close()
