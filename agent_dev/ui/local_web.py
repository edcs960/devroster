"""Common loopback HTTP boundary for local project tools."""

import hmac
import json
import logging
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.local_server import LocalHTTPServer
from agent_dev.ui.page_navigation import local_page_navigation
from agent_dev.ui.responses import send
from agent_dev.ui.shared_assets import NAVIGATION_ASSETS, shared_asset_name


class LocalWeb:
    prefix = ""
    page = ""

    def __init__(self, root):
        self.root = root
        self.token = secrets.token_urlsafe(32)
        self.assets = files("agent_dev.ui").joinpath("assets")

    @classmethod
    def matches(cls, path):
        return (
            shared_asset_name(path) is not None
            or path in NAVIGATION_ASSETS
            or path
            in {
                "/" + cls.page,
                "/" + cls.page + "/",
                "/" + cls.page + ".js",
                "/" + cls.page + ".css",
            }
            or path.startswith("/api/" + cls.prefix + "/")
        )

    def close(self):
        pass

    def get(self, action, query) -> Any:
        raise DomainError(ErrorCode.INVALID_INPUT, "페이지를 찾을 수 없습니다.")

    def post(self, action, payload) -> Any:
        raise DomainError(ErrorCode.INVALID_INPUT, "기능을 찾을 수 없습니다.")

    def handle(self, handler):
        try:
            parsed = urlsplit(handler.path)
            host = handler.headers.get("Host", "")
            port = handler.server.server_address[1]
            callback = (
                handler.command == "GET"
                and self.prefix == "integrations"
                and parsed.path == "/api/integrations/callback"
                and handler.headers.get("Sec-Fetch-Mode", "navigate") == "navigate"
            )
            if (
                host not in {f"127.0.0.1:{port}", f"localhost:{port}"}
                or not callback
                and handler.headers.get("Origin", "http://" + host) != "http://" + host
                or not callback
                and handler.headers.get("Sec-Fetch-Site", "") not in {"", "none", "same-origin"}
                and not local_page_navigation(handler, parsed.path, {"/" + self.page, "/" + self.page + "/"})
            ):
                send(handler, {"error": "같은 로컬 주소에서 접근해주세요."}, status=403)
                return
            values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30)
            if any(len(value) != 1 for value in values.values()):
                raise ValueError("duplicate query field")
            query = {k: v[0] for k, v in values.items()}
            if handler.command == "GET":
                shared = shared_asset_name(parsed.path)
                if shared is not None:
                    send(handler, self.assets.joinpath(shared).read_bytes(), "text/css")
                    return
                if parsed.path in NAVIGATION_ASSETS:
                    name, kind = NAVIGATION_ASSETS[parsed.path]
                    send(handler, self.assets.joinpath(name).read_bytes(), kind)
                    return
                page_asset = getattr(self, "page_assets", {}).get(parsed.path)
                if page_asset:
                    name, kind = page_asset
                    send(handler, self.assets.joinpath(name).read_bytes(), kind)
                    return
                if parsed.path in {
                    "/" + self.page,
                    "/" + self.page + "/",
                    "/" + self.page + ".js",
                    "/" + self.page + ".css",
                }:
                    suffix = parsed.path.rsplit(".", 1)[-1] if "." in parsed.path else "html"
                    kind = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[suffix]
                    send(
                        handler, self.assets.joinpath(self.page + "." + suffix).read_bytes(), kind
                    )
                else:
                    result = self.get(parsed.path.rsplit("/", 1)[-1], query)
                    if isinstance(result, tuple):  # (bytes, media type) e.g. owned screenshots
                        send(handler, result[0], result[1])
                        return
                    if parsed.path.endswith("/bootstrap"):
                        result = dict(result, token=self.token)
                    send(handler, result)
                return
            if handler.command != "POST" or not hmac.compare_digest(
                handler.headers.get("X-Local-Token", ""), self.token
            ):
                send(handler, {"error": "화면을 새로고침해주세요."}, status=403)
                return
            if handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
                send(handler, {"error": "JSON 요청이 필요합니다."}, status=415)
                return
            size = int(handler.headers.get("Content-Length", "0"))
            if not 0 < size <= 1_000_000:
                send(handler, {"error": "입력은 1MB 이하로 줄여주세요."}, status=413)
                return
            data = json.loads(handler.rfile.read(size))
            if not isinstance(data, dict):
                raise TypeError("object required")
            send(handler, self.post(parsed.path.rsplit("/", 1)[-1], data))
        except DomainError as exc:
            from agent_dev.adapters.preparation import PreparationModeError

            if isinstance(exc, PreparationModeError):
                send(handler, {"error": str(exc), "code": exc.code.value}, status=403)
                return
            send(
                handler,
                {"error": str(exc)},
                status=409 if exc.code == ErrorCode.STALE_VERSION else 400,
            )
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            send(handler, {"error": "입력 형식을 확인해주세요."}, status=400)
        except sqlite3.Error:
            # A database failure must still answer; a dropped connection reaches the page as a
            # bare "Failed to fetch". Details stay in the server log, not in the response.
            logging.getLogger(__name__).exception("Database error while handling %s", handler.path)
            send(
                handler,
                {"error": "작업 데이터를 읽지 못했습니다. 앱을 다시 시작한 뒤 다시 시도하세요."},
                status=503,
            )
        except OSError:
            send(handler, {"error": "파일/서버 접근에 실패했습니다."}, status=503)


def make_server(web, port=0):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(302)
                self.send_header("Location", "/" + web.page)
                self.end_headers()
            else:
                web.handle(self)

        def do_POST(self):
            web.handle(self)

        def log_message(self, format, *args):
            pass

    return LocalHTTPServer(("127.0.0.1", port), Handler)
