"""Wiki review page: approve, reject and retire knowledge revisions (CLI parity)."""

from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.local_web import LocalWeb


class WikiWeb(LocalWeb):
    prefix = page = "wiki"
    get_routes: ClassVar = {"/api/wiki/bootstrap", "/api/wiki/items"}
    post_routes: ClassVar = {"/api/wiki/approve", "/api/wiki/reject", "/api/wiki/retire"}

    def __init__(self, root):
        from agent_dev.adapters.workflow_runtime import project_root
        from agent_dev.adapters.workspace import configuration_root

        super().__init__(root)
        self._config = configuration_root(Path(root))
        self._root = project_root(self._config)

    def _wiki(self, read_only=True):
        from agent_dev.adapters.secrets import registered_secrets
        from agent_dev.adapters.state.sqlite.store import WorkflowRepository
        from agent_dev.adapters.wiki_knowledge import WikiKnowledge
        from agent_dev.adapters.workspace_locator import state_db_path

        path = state_db_path(self._config)
        if not path.is_file():
            raise DomainError(
                ErrorCode.INVALID_INPUT, "프로젝트 설정에서 작업 공간을 먼저 준비해주세요."
            )
        repository = WorkflowRepository(
            Database(path, read_only=read_only), secrets=registered_secrets(self._config)
        )
        return WikiKnowledge(repository, self._root)

    def handle(self, handler):
        path = urlsplit(handler.path).path
        allowed = self.get_routes if handler.command == "GET" else self.post_routes
        if path.startswith("/api/") and path not in allowed:
            from agent_dev.ui.studio_server import StudioWeb

            StudioWeb.send(handler, {"error": "요청 경로를 확인해주세요."}, status=400)
            return
        super().handle(handler)

    def get(self, action, query):
        from agent_dev.adapters.wiki_knowledge import PAGES

        wiki = self._wiki()
        if action == "bootstrap":
            projects = [
                {"id": row["id"], "name": row["name"]}
                for row in wiki.repository.db.rows("SELECT id,name FROM projects ORDER BY id")
            ]
            return {
                "projects": projects,
                "workspace_project": wiki.workspace_project(),
                "pages": PAGES,
            }
        project_id = query.get("project_id", "")
        if not project_id:
            raise DomainError(ErrorCode.INVALID_INPUT, "project_id가 필요합니다.")
        return {"items": wiki.items(project_id)}

    def post(self, action, payload):
        project_id, item_id = payload.get("project_id", ""), payload.get("item_id", "")
        actor = str(payload.get("actor", "")).strip()
        if not project_id or not item_id:
            raise DomainError(ErrorCode.INVALID_INPUT, "프로젝트와 항목을 선택해주세요.")
        if not actor:
            raise DomainError(ErrorCode.INVALID_INPUT, "처리자 이름을 입력해주세요.")
        wiki = self._wiki(read_only=False)
        if action == "retire":
            result = wiki.retire(project_id, item_id, str(payload.get("reason", "")), actor)
        else:
            revision = payload.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise DomainError(ErrorCode.INVALID_INPUT, "revision이 필요합니다.")
            method = wiki.approve if action == "approve" else wiki.reject
            result = method(project_id, item_id, revision, actor)
        # The Markdown view follows the DB; a failed render keeps the decision.
        if project_id == wiki.workspace_project():
            try:
                result["rendered"] = wiki.render(project_id)
            except (DomainError, OSError) as error:
                result["render_error"] = str(error)
        return result
