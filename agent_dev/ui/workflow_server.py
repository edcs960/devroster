"""Loopback Workflow editor; can be mounted beside SetupWeb/StudioWeb."""

import hmac
import json
import secrets
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_dev.adapters.workflow_definitions import WorkflowDefinitions
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.role_policy import ROLES
from agent_dev.domain.workflow_graph import WorkflowValidationError
from agent_dev.ui.shared_assets import NAVIGATION_ASSETS, shared_asset_name

ASSETS = {
    "/workflows": ("workflows.html", "text/html"),
    "/workflows/": ("workflows.html", "text/html"),
    "/workflows.js": ("workflows.js", "text/javascript"),
    "/workflows.css": ("workflows.css", "text/css"),
}


class WorkflowWeb:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self._service = None
        self.token = secrets.token_urlsafe(32)
        self.assets = files("agent_dev.ui").joinpath("assets")

    @property
    def service(self) -> WorkflowDefinitions:
        if self._service is None:
            self._service = WorkflowDefinitions(self.root)
        return self._service

    def read_service(self):
        from types import SimpleNamespace

        from agent_dev.adapters.state.sqlite.database import Database
        from agent_dev.adapters.state.sqlite.store import WorkflowRepository
        from agent_dev.adapters.storage_registry import state_db_path

        path = state_db_path(self.root)
        if not path.is_file():
            return None
        return WorkflowDefinitions(
            runtime=SimpleNamespace(store=WorkflowRepository(Database(path, read_only=True)))
        )

    @staticmethod
    def matches(path):
        return (
            shared_asset_name(path) is not None
            or path in NAVIGATION_ASSETS
            or path in ASSETS
            or path.startswith("/api/workflows/")
        )

    def close(self):
        pass

    @staticmethod
    def send(handler, data, kind="application/json", status=200):
        if kind == "application/json":
            data = json.dumps(data, ensure_ascii=False).encode()
        handler.send_response(status)
        for key, value in {
            "Content-Type": kind + "; charset=utf-8",
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        }.items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(data)

    def handle(self, handler):
        try:
            parsed = urlparse(handler.path)
            host = handler.headers.get("Host", "")
            port = handler.server.server_address[1]
            if (
                host not in {f"127.0.0.1:{port}", f"localhost:{port}"}
                or handler.headers.get("Origin", "http://" + host) != "http://" + host
                or handler.headers.get("Sec-Fetch-Site", "") not in {"", "none", "same-origin"}
            ):
                raise DomainError(ErrorCode.AUTH_REQUIRED, "Loopback same-origin access required.")
            if handler.command == "GET":
                shared = shared_asset_name(parsed.path)
                if shared is not None:
                    self.send(handler, self.assets.joinpath(shared).read_bytes(), "text/css")
                    return
                if parsed.path in NAVIGATION_ASSETS:
                    name, kind = NAVIGATION_ASSETS[parsed.path]
                    self.send(handler, self.assets.joinpath(name).read_bytes(), kind)
                    return
                if parsed.path in ASSETS:
                    name, kind = ASSETS[parsed.path]
                    self.send(handler, self.assets.joinpath(name).read_bytes(), kind)
                    return
                query = parse_qs(parsed.query)
                project = query.get("project_id", query.get("id", [""]))[0]
                workflow = query.get("workflow_id", [""])[0]
                raw_version = query.get("version", [None])[0]
                version = int(raw_version) if raw_version is not None else None
                operation = parsed.path.removeprefix("/api/workflows/")
                service = self.read_service()
                if operation != "bootstrap" and service is None:
                    raise DomainError(ErrorCode.INVALID_INPUT, "프로젝트 설정을 먼저 저장해주세요.")
                if operation == "bootstrap":
                    result = {
                        "token": self.token,
                        "presets": WorkflowDefinitions.presets(),
                        "roles": sorted(ROLES),
                        "projects": service.projects() if service else [],
                    }
                elif service is None:
                    raise DomainError(ErrorCode.INVALID_INPUT, "프로젝트 설정을 먼저 저장해주세요.")
                elif operation == "list":
                    result = {"workflows": service.list(project)}
                elif operation == "detail":
                    result = service.show(project, workflow, version)
                elif operation == "diff":
                    base = query.get("base", [""])[0]
                    if not base.isdigit() or raw_version is None:
                        raise DomainError(ErrorCode.INVALID_INPUT, "비교할 두 버전을 선택해주세요.")
                    result = service.diff(project, workflow, int(base), version)
                elif operation == "export":
                    result = service.export(project, workflow, version)
                elif operation in {"execution", "executionstate", "execution-state"}:
                    result = service.execution_state(query.get("task_id", [""])[0])
                else:
                    self.send(handler, {"error": "NOT_FOUND"}, status=404)
                    return
                self.send(handler, result)
                return
            if handler.command != "POST":
                self.send(handler, {"error": "METHOD_NOT_ALLOWED"}, status=405)
                return
            if not hmac.compare_digest(handler.headers.get("X-Workflow-Token", ""), self.token):
                raise DomainError(ErrorCode.AUTH_REQUIRED, "Workflow CSRF token required.")
            if handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise DomainError(ErrorCode.INVALID_INPUT, "JSON content type required.")
            size = int(handler.headers.get("Content-Length", "0"))
            if not 0 < size <= 1_000_000:
                raise DomainError(ErrorCode.INVALID_INPUT, "Workflow JSON exceeds 1MB or is empty.")

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON key")
                    result[key] = value
                return result

            body = json.loads(handler.rfile.read(size), object_pairs_hook=unique)
            if not isinstance(body, dict):
                raise DomainError(ErrorCode.INVALID_INPUT, "JSON object required.")
            operation = parsed.path.removeprefix("/api/workflows/")
            project = body.get("project_id")
            if operation == "validate":
                result = self.service.validate(body.get("document"), project)
            elif operation == "preview":
                result = self.service.preview(body.get("document"), body.get("metadata"), project)
            elif operation == "save":
                result = self.service.save(
                    project, body.get("document"), body.get("expected_version", 0)
                )
            elif operation == "apply":
                result = self.service.apply(
                    project,
                    body.get("workflow_id"),
                    body.get("expected_profile_version"),
                    body.get("version"),
                )
            elif operation == "import":
                result = self.service.import_document(
                    project, body.get("document"), body.get("remap")
                )
            elif operation == "approve":
                result = self.service.approve(
                    body.get("task_id"),
                    body.get("node_id"),
                    body.get("actor"),
                    body.get("decision", "approved"),
                    body.get("expected_version"),
                    body.get("note", ""),
                )
            else:
                self.send(handler, {"error": "NOT_FOUND"}, status=404)
                return
            self.send(handler, result)
        except DomainError as exc:
            self.send(
                handler,
                {"error": exc.code.value, "message": str(exc)}
                | (
                    {"location": exc.location}
                    if isinstance(exc, WorkflowValidationError) and exc.location
                    else {}
                ),
                status=409
                if exc.code == ErrorCode.STALE_VERSION
                else 403
                if exc.code in {ErrorCode.AUTH_REQUIRED, ErrorCode.POLICY_VIOLATION}
                else 400,
            )
        except (ValueError, TypeError, KeyError, RecursionError, json.JSONDecodeError):
            self.send(
                handler,
                {"error": "INVALID_INPUT", "message": "Invalid Workflow request fields."},
                status=400,
            )
