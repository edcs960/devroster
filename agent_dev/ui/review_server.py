"""Scoped CSRF-protected code review APIs for a task's registered worktrees."""

from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from agent_dev.adapters import development_review as review
from agent_dev.adapters import preview_host as preview
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.local_web import LocalWeb


class ReviewWeb(LocalWeb):
    prefix = page = "review"
    get_routes: ClassVar = {
        "/api/review/bootstrap",
        "/api/review/diff",
        "/api/review/evidence",
        "/api/review/preview",
        "/api/review/preview-detail",
        "/api/review/preview-screenshot",
    }
    post_routes: ClassVar = {
        "/api/review/comment",
        "/api/review/comment-state",
        "/api/review/decision",
        "/api/review/preview-prepare",
        "/api/review/preview-start",
        "/api/review/preview-capture",
        "/api/review/preview-stop",
        "/api/review/preview-comment",
        "/api/review/preview-decision",
    }

    def __init__(self, root):
        from agent_dev.adapters.workflow_runtime import project_root
        from agent_dev.adapters.workspace import configuration_root

        super().__init__(root)
        self._config = configuration_root(Path(root))
        self._root = project_root(self._config)
        self._agent_port = 0

    def close(self):
        preview.shutdown(self.token)  # Only processes this server started.

    @classmethod
    def matches(cls, path):
        return path in {"/review", "/review/", "/review.js", "/review.css"} or path.startswith(
            "/api/review/"
        )

    def _db(self, read_only):
        from agent_dev.adapters.storage_registry import state_db_path

        path = state_db_path(self._config)
        if not path.is_file():
            raise DomainError(
                ErrorCode.INVALID_INPUT, "프로젝트 설정에서 작업 공간을 먼저 준비해주세요."
            )
        return Database(path, read_only=read_only)

    def _context(self, task_id, read_only=True):
        from agent_dev.adapters.secrets import registered_secrets

        return review.ReviewContext(self._db(read_only), task_id, registered_secrets(self._config))

    def _preview(self, task_id, read_only=True):
        from agent_dev.adapters.secrets import registered_secrets

        return preview.PreviewContext(
            self._db(read_only), task_id, registered_secrets(self._config), self._config
        )

    def handle(self, handler):
        path = urlsplit(handler.path).path
        self._agent_port = handler.server.server_address[1]
        allowed = self.get_routes if handler.command == "GET" else self.post_routes
        if path.startswith("/api/") and path not in allowed:
            from agent_dev.ui.studio_server import StudioWeb

            StudioWeb.send(handler, {"error": "요청 경로를 확인해주세요."}, status=400)
            return
        super().handle(handler)

    def get(self, action, query):
        if action.startswith("preview"):
            context = self._preview(query.get("task", ""))
            if action == "preview":
                return preview.bootstrap(context, self._agent_port)
            if action == "preview-detail":
                return preview.capture_detail(context, query.get("capture", ""))
            return (preview.load_capture(context, query.get("capture", ""))[1], "image/png")
        context = self._context(query.get("task", ""))
        if action == "bootstrap":
            return review.bootstrap(context)
        if action == "diff":
            return review.file_diff(
                context, query.get("module", ""), query.get("stage", ""), query.get("path", "")
            )
        if action == "evidence":
            return review.evidence(context)
        raise DomainError(ErrorCode.INVALID_INPUT, "조회 기능을 찾을 수 없습니다.")

    def post(self, action, payload):
        if action.startswith("preview-"):
            live = self._preview(payload.get("task", ""), read_only=False)
            handlers = {
                "preview-prepare": lambda: preview.prepare(live, payload, self._agent_port),
                "preview-start": lambda: preview.start(live, payload, self._agent_port, self.token),
                "preview-capture": lambda: preview.capture(live, payload),
                "preview-stop": lambda: preview.stop(live, payload),
                "preview-comment": lambda: preview.add_comment(live, payload),
                "preview-decision": lambda: preview.decide(live, payload),
            }
            if action in handlers:
                return handlers[action]()
        context = self._context(payload.get("task", ""), read_only=False)
        if action == "comment":
            return review.add_comment(context, payload)
        if action == "comment-state":
            return review.set_comment_state(context, payload)
        if action == "decision":
            return review.decide(context, payload)
        raise DomainError(ErrorCode.INVALID_INPUT, "실행 기능을 찾을 수 없습니다.")
