"""Own loopback servers per project, without replacing an active workspace."""

import threading

from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.workspace_registry import configuration_for, register_root, root_path, roots


class WorkspaceSessions:
    def __init__(self):
        self.lock = threading.RLock()
        self.servers = {}
        self.children = []
        self.closed = False

    def bind(self, server, *, owner=False):
        root = str(ProjectSetup(server.studio.root).root)
        with self.lock:
            self.servers[root] = server
            server.studio.setup.workspace_sessions = self
            server.studio.workspace_sessions = self if owner else None

    def open(self, value, current):
        root = ProjectSetup(root_path(value)).root
        original_current = current
        if not root.is_dir():
            raise ValueError("폴더가 없거나 이동되었습니다. 기존 폴더를 다시 등록해주세요.")
        current = str(ProjectSetup(current).root)
        if str(root) not in [current, *roots()]:
            raise ValueError("프로젝트 폴더를 먼저 등록해주세요.")
        # Resolve preparation state before starting anything or updating last-used state.
        configuration = configuration_for(root)
        settings = ProjectSetup(configuration).read()
        with self.lock:
            if self.closed:
                raise ValueError("작업 공간이 종료되었습니다. 앱을 다시 열어주세요.")
            server = self.servers.get(str(root))
            created = server is None
            thread = None
            try:
                if created:
                    from agent_dev.ui.studio_server import make_server

                    server = make_server(configuration, port=0)
                    self.bind(server)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    self.children.append((server, thread))
                # Keep the starting workspace reachable from the newly opened one.
                if root_path(current).is_dir():
                    register_root(original_current)
                register_root(configuration, remember=True)
            except Exception:
                # Only this attempt's new server belongs to the failed open.
                if created and server is not None:
                    self.servers.pop(str(root), None)
                    self.children = [pair for pair in self.children if pair[0] is not server]
                    try:
                        if thread is not None and thread.ident is not None:
                            server.shutdown()
                            thread.join(timeout=5)
                    finally:
                        try:
                            server.studio.close()
                        finally:
                            server.server_close()
                raise
            path = "/agent" if settings["revision"] > 0 else "/setup"
            return {"root": str(root), "url": f"http://127.0.0.1:{server.server_port}{path}"}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            children, self.children = self.children, []
        for server, thread in children:
            server.shutdown()
            server.studio.close()
            server.server_close()
            thread.join(timeout=5)
