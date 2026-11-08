"""Loopback project setup UI shared with Studio and the legacy web server."""

import hmac
import json
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent_dev.adapters.ai_providers import metadata
from agent_dev.adapters.model_catalog import codex_models
from agent_dev.adapters.presets import PRESETS
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.skill_cli import listing as skill_listing
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.project_roles import catalog as role_catalog
from agent_dev.ui.page_navigation import local_page_navigation
from agent_dev.ui.studio_server import StudioWeb

ASSETS = {
    "/setup": ("setup.html", "text/html"),
    "/setup/": ("setup.html", "text/html"),
    "/setup.js": ("setup.js", "text/javascript"),
    "/setup.css": ("setup.css", "text/css"),
    "/settings": ("settings.html", "text/html"),
    "/settings/": ("settings.html", "text/html"),
    "/settings.js": ("settings.js", "text/javascript"),
    "/ai": ("ai.html", "text/html"),
    "/ai/": ("ai.html", "text/html"),
    "/ai.js": ("ai.js", "text/javascript"),
}


class SetupHTTPServer(ThreadingHTTPServer):
    studio: StudioWeb


class SetupWeb:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.workspace_sessions = None
        self.token = secrets.token_urlsafe(32)
        self.assets = files("agent_dev.ui").joinpath("assets")
        from agent_dev.ui.feature_routes import FeatureRoutes
        self.features = FeatureRoutes(root)

    def close(self):
        from agent_dev.adapters import cli_login

        cli_login.shutdown(self.token)
        self.features.close()

    @staticmethod
    def matches(path):
        from agent_dev.ui.feature_routes import FeatureRoutes
        return (
            path in ASSETS
            or path.startswith(("/api/setup/", "/api/settings/"))
            or FeatureRoutes.matches(path)
        )

    def send(self, handler, data, kind="application/json", status=200):
        StudioWeb.send(handler, data, kind, status)

    def handle(self, handler):
        from urllib.parse import urlsplit
        if self.features.matches(urlsplit(handler.path).path):
            return self.features.handle(handler)
        try:
            parsed = urlparse(handler.path)
            host = handler.headers.get("Host", "")
            port = handler.server.server_address[1]
            if (
                host not in {f"127.0.0.1:{port}", f"localhost:{port}"}
                or handler.headers.get("Origin", "http://" + host) != "http://" + host
                or (handler.headers.get("Sec-Fetch-Site", "") not in {"", "none", "same-origin"}
                    and not local_page_navigation(
                        handler, parsed.path, {"/setup", "/setup/", "/settings", "/settings/", "/ai", "/ai/"}
                    ))
            ):
                self.send(handler, {"error": "같은 로컬 주소에서 접근해주세요."}, status=403)
                return
            if handler.command == "GET":
                if parsed.path == "/favicon.ico":
                    self.send(handler, b"", "image/x-icon", status=204)
                    return
                if parsed.path in ASSETS:
                    name, kind = ASSETS[parsed.path]
                    self.send(handler, self.assets.joinpath(name).read_bytes(), kind)
                elif parsed.path == "/api/setup/bootstrap":
                    root = parse_qs(parsed.query).get("root", [str(self.root)])[0]
                    self.send(
                        handler,
                        {
                            "token": self.token,
                            "settings": ProjectSetup(Path(root)).read(),
                            "presets": PRESETS,
                            "model_catalog": {"codex_cli": codex_models()},
                            "providers": metadata(),
                            "role_catalog": role_catalog(),
                            "skill_catalog": skill_listing(),
                        },
                    )
                elif parsed.path == "/api/settings/ai":
                    from agent_dev.adapters import ai_tools_service

                    self.send(handler, dict(ai_tools_service.state(), token=self.token))
                elif parsed.path in SETTINGS_READS:
                    self.send(handler, settings_read(self, parsed))
                elif parsed.path == "/api/setup/projects":
                    from agent_dev.adapters.workspace_registry import project_list
                    self.send(handler, dict(project_list(self.root), token=self.token))
                elif parsed.path == "/api/setup/folders":
                    path = (
                        Path(parse_qs(parsed.query).get("path", [str(self.root)])[0])
                        .expanduser()
                        .resolve()
                    )
                    if not path.is_dir():
                        raise ValueError("not a folder")
                    children = sorted(
                        (
                            p
                            for p in path.iterdir()
                            if p.is_dir() and not p.name.startswith(".") and not p.is_symlink()
                        ),
                        key=lambda p: p.name.casefold(),
                    )
                    self.send(
                        handler,
                        {
                            "path": str(path),
                            "parent": str(path.parent),
                            "directories": [
                                {"name": p.name, "path": str(p)} for p in children[:200]
                            ],
                            "truncated": len(children) > 200,
                        },
                    )
                else:
                    self.send(handler, {"error": "페이지를 찾을 수 없습니다."}, status=404)
                return
            if handler.command != "POST" or not hmac.compare_digest(
                handler.headers.get("X-Setup-Token", ""), self.token
            ):
                self.send(handler, {"error": "화면을 새로고침한 뒤 다시 시도해주세요."}, status=403)
                return
            if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.send(handler, {"error": "JSON 요청이 필요합니다."}, status=415)
                return
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                self.send(handler, {"error": "입력은 1MB 이하로 줄여주세요."}, status=413)
                return
            payload = json.loads(handler.rfile.read(length).decode("utf-8"))
            if parsed.path in AI_WRITES:
                self.send(handler, ai_write(parsed.path, payload, owner=self.token))
                return
            if parsed.path in SETTINGS_WRITES:
                self.send(handler, settings_write(parsed.path, payload))
                return
            if parsed.path in {"/api/setup/project-register", "/api/setup/project-open"}:
                from agent_dev.adapters.workspace_registry import (
                    project_list,
                    register_root,
                    root_path,
                )
                root = root_path(payload["root"])
                if parsed.path.endswith("project-register"):
                    ProjectSetup(root).read()
                    register_root(root)
                    result = project_list(self.root)
                else:
                    if self.workspace_sessions is None:
                        raise ValueError("현재 서버에서 작업 공간 전환을 지원하지 않습니다.")
                    result = self.workspace_sessions.open(str(root), self.root)
                self.send(handler, result)
                return
            service = ProjectSetup(Path(payload["root"]))
            if parsed.path == "/api/setup/save":
                first = service.current_profile()[2] is None
                result = service.save(
                    payload["settings"], check_auth=payload.get("check_auth") is True
                )
                from agent_dev.adapters.workspace_registry import register_root

                register_root(service.root)
                if first and payload["settings"].get("ai_tools") is None:
                    result = first_shared_ai(service.root, result)
            elif parsed.path == "/api/setup/skills/status":
                from agent_dev.adapters.skill_cli import status, suggestions

                result = {
                    "suggestions": suggestions(service.config, payload.get("modules")),
                    "status": status(service.config),
                }
            elif parsed.path == "/api/setup/skills/sync":
                from agent_dev.adapters.skill_cli import sync

                result = sync(service.config)
            elif parsed.path in {
                "/api/setup/rules",
                "/api/setup/rules/attach",
                "/api/setup/rules/remove",
                "/api/setup/rules/sync",
            }:
                from agent_dev.adapters import ai_rules

                # Rules live with the project's source (.ai/rules), not in the engine folder.
                action = parsed.path.rsplit("/", 1)[1]
                if action == "attach":
                    ai_rules.attach(
                        service.root, payload["name"], payload["content"], payload.get("replace") is True
                    )
                elif action == "remove":
                    ai_rules.remove(service.root, payload["name"])
                elif action == "sync":
                    synced = ai_rules.sync(service.root)
                    if not synced["ok"]:
                        raise DomainError(
                            ErrorCode.INVALID_INPUT,
                            "표시 영역이 깨진 파일은 쓰지 않았습니다: " + ", ".join(synced["stale"]),
                        )
                result = ai_rules.overview(service.root)
            elif parsed.path in {"/api/setup/hooks", "/api/setup/hooks/approve"}:
                from agent_dev.adapters import ai_hooks

                # Approval is kept with this user's engine settings, never in the repository.
                if parsed.path.endswith("approve"):
                    ai_hooks.approve(service.config, service.root, str(payload.get("digest", "")))
                result = dict(
                    ai_hooks.overview(service.root, service.config),
                    recent=ai_hooks.recent(service.config),
                )
            elif parsed.path == "/api/setup/preset-gates":
                result = service.preset_gates(payload.get("modules"))
            elif parsed.path == "/api/setup/check":
                result = service.check(check_auth=payload.get("check_auth") is True)
            elif parsed.path == "/api/setup/preview":
                result = service.preview(payload["settings"], payload["task"], payload.get("phase_id", "developer"))
            elif parsed.path in {
                "/api/setup/cli-auth",
                "/api/setup/cli-login",
                "/api/setup/cli-login-status",
                "/api/setup/cli-login-cancel",
            }:
                result = cli_login_request(
                    service, parsed.path.rsplit("/", 1)[1], payload, owner=self.token
                )
            else:
                self.send(handler, {"error": "기능을 찾을 수 없습니다."}, status=404)
                return
            self.send(handler, result)
        except DomainError as exc:
            self.send(
                handler,
                {"error": str(exc)},
                status=409 if exc.code == ErrorCode.STALE_VERSION else 400,
            )
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            self.send(handler, {"error": "입력 형식과 폴더 경로를 확인해주세요."}, status=400)
        except (OSError, sqlite3.Error):
            self.send(
                handler,
                {"error": "설정을 읽거나 저장하지 못했습니다. 폴더 접근 권한을 확인해주세요."},
                status=503,
            )


SETTINGS_READS = {"/api/settings/global", "/api/settings/effective"}
SETTINGS_WRITES = {
    "/api/settings/global",
    "/api/settings/apply-global",
    "/api/settings/connections",
    "/api/settings/connections/disable",
    "/api/settings/connections/bind",
    "/api/settings/storage/plan",
    "/api/settings/storage/apply",
    "/api/settings/storage/recover",
}


AI_WRITES = {
    "/api/settings/ai",
    "/api/settings/ai/auth-check",
    "/api/settings/ai/login",
    "/api/settings/ai/login-status",
    "/api/settings/ai/login-cancel",
    "/api/settings/ai/key",
    "/api/settings/ai/import",
}


def first_shared_ai(root, result):
    """The first project on this computer makes its AI tools the shared ones.

    Login, models and keys live in the AI tools menu for every project; without a shared
    revision a new project kept its own copy and the menu showed none (first-use review).
    """
    from agent_dev.adapters import ai_tools_service
    from agent_dev.adapters.global_ai import GlobalAi
    from agent_dev.adapters.project_setup import ProjectSetup

    if GlobalAi().read()["revision"]:
        return result
    report = ai_tools_service.import_project(root, apply=True)
    return dict(result, settings=ProjectSetup(root).read(), shared_ai=report)


def ai_write(path, payload, owner=None):
    """AI tools screen; key values are accepted but never returned."""
    from agent_dev.adapters import ai_tools_service as service

    if path == "/api/settings/ai":
        return service.save(payload["values"], payload["expected_revision"])
    if path == "/api/settings/ai/auth-check":
        return {"auth": service.check_auth()}
    if path == "/api/settings/ai/key":
        return {"keys": service.set_key(payload["name"], payload.get("value"), delete=payload.get("delete") is True)}
    if path == "/api/settings/ai/import":
        return service.import_project(Path(payload["root"]), apply=payload.get("apply") is True)
    operation = {"login": "start", "login-status": "status", "login-cancel": "cancel"}[
        path.rsplit("/", 1)[1]
    ]
    return service.login(operation, payload, owner=owner)


def settings_read(web, parsed):
    """Shared settings, connection definitions and locations; never credential values."""
    from agent_dev.adapters.global_settings import GlobalSettings
    from agent_dev.adapters.shared_integrations import SharedIntegrations
    from agent_dev.adapters.storage_registry import location_report

    if parsed.path == "/api/settings/effective":
        root = Path(parse_qs(parsed.query).get("root", [str(web.root)])[0])
        return dict(ProjectSetup(root).effective(), location=location_report(root))
    return {
        "token": web.token,
        "settings": GlobalSettings().read(),
        "connections": SharedIntegrations().definitions(),
        "location": location_report(web.root),
    }


def settings_write(path, payload):
    """Each route calls the P3/P4/P6 service; no settings logic lives in the web layer."""
    from agent_dev.adapters import storage_migration
    from agent_dev.adapters.global_settings import GlobalSettings
    from agent_dev.adapters.shared_integrations import SharedIntegrations
    from agent_dev.adapters.storage_registry import workspace_locations
    from agent_dev.adapters.workspace import configuration_root

    if path == "/api/settings/global":
        return GlobalSettings().save(payload["values"], payload["expected_revision"])
    if path == "/api/settings/apply-global":
        result = ProjectSetup(Path(payload["root"])).apply_global(
            payload["global_revision"],
            payload["expected_revision"],
            overrides=payload.get("overrides"),
            templates=payload.get("templates") is True,
        )
        return {k: result[k] for k in ("profile_version", "global_revision", "warnings")}
    if path == "/api/settings/storage/plan":
        scope = payload["scope"]
        source = (
            Path(payload["source"]) if scope == "workspace" else storage_migration.management_root()
        )
        return storage_migration.plan_move(source, Path(payload["destination"]), scope)
    if path == "/api/settings/storage/apply":
        return storage_migration.apply_move(
            payload["plan_id"], payload["expected_registry_revision"]
        )
    if path == "/api/settings/storage/recover":
        return storage_migration.recover_move(payload["migration_id"], payload["action"])
    if path == "/api/settings/connections/bind":
        root = Path(payload["root"])
        shared = SharedIntegrations(root)
        return shared.bind(
            workspace_locations(configuration_root(root)).workspace_id,
            payload["local_id"],
            payload["connection_id"],
            payload["version"],
            payload["scopes"],
            payload["operations"],
            payload["expected_version"],
            credential_ref=payload.get("credential_ref", ""),
        )
    shared = SharedIntegrations()
    if path == "/api/settings/connections/disable":
        return shared.disable(payload["connection_id"], payload["expected_revision"])
    return shared.register(
        payload["connection_id"], payload["config"], payload.get("expected_version", 0)
    )


def cli_login_request(service, operation, payload, owner=None):
    """App-private CLI login: commands come from the saved runner settings only."""
    from agent_dev.adapters import cli_login
    from agent_dev.adapters.project_setup import auth_probe

    runners = service.read()["ai"]["runners"]
    owner_scope = (
        (owner, str(service.root.resolve())) if owner is not None and hasattr(service, "root") else owner
    )

    def command(provider):
        return str(runners.get(provider, {}).get("command") or provider.removesuffix("_cli"))

    if operation == "cli-auth":
        return {p: auth_probe(p, command(p)) for p in cli_login.LOGIN_ARGS}
    if operation == "cli-login":
        provider = str(payload.get("provider", ""))
        return cli_login.start(
            provider, command(provider) if provider in cli_login.LOGIN_ARGS else "", owner=owner_scope
        )
    job = (cli_login.cancel if operation == "cli-login-cancel" else cli_login.status)(
        payload.get("job_id"), owner=owner_scope
    )
    if job["done"]:
        job["logged_in"] = cli_login.auth_result(job["job_id"], auth_probe, owner=owner_scope)
    return job


def make_server(root, port=8768):
    web = StudioWeb(root)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.end_headers()
                return
            web.handle(self)

        def do_POST(self):
            web.handle(self)

        def log_message(self, format, *args):
            pass

    server = SetupHTTPServer(("127.0.0.1", port), Handler)
    server.studio = web
    from agent_dev.ui.workspace_sessions import WorkspaceSessions
    WorkspaceSessions().bind(server, owner=True)
    return server


def serve(root, port=8768):
    server = make_server(root, port)
    print(f"프로젝트·AI·환경 설정: http://127.0.0.1:{server.server_port}/setup")
    print("종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.studio.close()
        server.server_close()
