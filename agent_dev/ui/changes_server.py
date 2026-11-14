"""Scoped CSRF-protected requirement change preview/propose/apply/reject APIs."""

from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from agent_dev.adapters import requirement_changes as rc
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.local_web import LocalWeb


class ChangesWeb(LocalWeb):
    prefix = page = "changes"
    get_routes: ClassVar = {"/api/changes/bootstrap", "/api/changes/query", "/api/changes/detail"}
    post_routes: ClassVar = {
        "/api/changes/preview",
        "/api/changes/propose",
        "/api/changes/apply",
        "/api/changes/reject",
    }

    def __init__(self, root):
        from agent_dev.adapters.workflow_runtime import project_root
        from agent_dev.adapters.workspace import configuration_root

        super().__init__(root)
        self._config = configuration_root(Path(root))
        self._root = project_root(self._config)

    @classmethod
    def matches(cls, path):
        return path in {"/changes", "/changes/", "/changes.js", "/changes.css"} or any(
            path.startswith(p) for p in ("/api/changes/",)
        )

    def _db(self, read_only=True):
        from agent_dev.adapters.workspace_locator import state_db_path

        path = state_db_path(self._config)
        if not path.is_file():
            raise DomainError(
                ErrorCode.INVALID_INPUT, "프로젝트 설정에서 작업 공간을 먼저 준비해주세요."
            )
        return Database(path, read_only=read_only)

    def handle(self, handler):
        path = urlsplit(handler.path).path
        allowed = self.get_routes if handler.command == "GET" else self.post_routes
        if path.startswith("/api/") and path not in allowed:
            from agent_dev.ui.responses import send

            send(handler, {"error": "요청 경로를 확인해주세요."}, status=400)
            return
        super().handle(handler)

    def get(self, action, query):
        if action == "bootstrap":
            return rc.bootstrap(self._db(), query, self._root)
        if action == "query":
            return rc.query(self._db(), query)
        if action == "detail":
            change_id = query.get("change_id", "")
            if not change_id:
                raise DomainError(ErrorCode.INVALID_INPUT, "change_id가 필요합니다.")
            return rc.detail(self._db(), change_id, query.get("project_id"))
        raise DomainError(ErrorCode.INVALID_INPUT, "조회 기능을 찾을 수 없습니다.")

    def post(self, action, payload):
        if action == "preview":
            return rc.preview(self._db(), self._root, payload)
        if action == "propose":
            return rc.propose(self._db(False), self._root, payload)
        if action == "apply":
            return rc.apply(self._db(False), self._root, payload)
        if action == "reject":
            return rc.reject(self._db(False), payload)
        raise DomainError(ErrorCode.INVALID_INPUT, "실행 기능을 찾을 수 없습니다.")
