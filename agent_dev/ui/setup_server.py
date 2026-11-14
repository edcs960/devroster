"""Loopback project setup screen served on its own (the screen itself is in setup_web)."""

from http.server import BaseHTTPRequestHandler

from agent_dev.ui.local_server import LocalHTTPServer
from agent_dev.ui.setup_web import (  # noqa: F401 - names kept importable from here
    AI_WRITES,
    ASSETS,
    PRICES,
    SETTINGS_READS,
    SETTINGS_WRITES,
    SetupWeb,
    ai_write,
    cli_login_request,
    first_shared_ai,
    settings_read,
    settings_write,
    worktree_cleanup,
)
from agent_dev.ui.studio_server import StudioWeb


class SetupHTTPServer(LocalHTTPServer):
    studio: StudioWeb


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
    from agent_dev.ui.studio_server import make_server as studio_server
    from agent_dev.ui.workspace_sessions import WorkspaceSessions

    WorkspaceSessions(studio_server).bind(server, owner=True)
    return server


def serve(root, port=8768):
    server = make_server(root, port)
    from agent_dev.ui.session import launch_url

    # The page only answers a browser that came through this one-time address.
    print(f"프로젝트·AI·환경 설정: {launch_url(f'http://127.0.0.1:{server.server_port}', '/setup')}")
    print("종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.studio.close()
        server.server_close()
