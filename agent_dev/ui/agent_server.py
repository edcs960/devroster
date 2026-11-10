"""Web operator console with the shared loopback and CSRF boundary."""

from agent_dev.adapters.agent_console import AgentConsole, catalog
from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.local_web import LocalWeb


class AgentWeb(LocalWeb):
    prefix = page = "agent"

    def __init__(self, root, dispatcher=None):
        super().__init__(root)
        self.console = AgentConsole(root, dispatcher)

    def close(self):
        self.console.close()

    def get(self, action, query):
        if action == "bootstrap":
            return {
                "catalog": catalog(),
                "workspace": str(self.console.workspace),
                "tasks": TaskQueries(self.root).tasks(100),
                "jobs": self.console.history(),
                "context": self.console.context(),
            }
        if action == "history":
            return {"jobs": self.console.history(query.get("limit", 50))}
        if action == "tasks":
            return TaskQueries(self.root).tasks(
                100, search=query.get("search", ""), page=int(query.get("page", 1))
            )
        if action == "task":
            return TaskQueries(self.root).explain(query["id"])
        if action == "catalog":
            return catalog()
        if action == "execution":
            from agent_dev.adapters.execution_view import view

            cursor = query.get("cursor", "0")
            return view(self.root, query.get("task", ""), int(cursor) if cursor.isdigit() else -1)
        if action == "log":
            from agent_dev.adapters.execution_view import live_log

            offset = query.get("offset", "0")
            return live_log(
                self.root,
                query.get("task", ""),
                int(offset) if offset.isdigit() else -1,
                query.get("attempt", ""),
            )
        raise DomainError(ErrorCode.INVALID_INPUT, "조회 기능을 찾을 수 없습니다.")

    def post(self, action, payload):
        if action == "preview":
            return self.console.preview(payload)
        if action == "submit":
            return self.console.start(payload)
        if action == "cancel":
            return self.console.cancel(payload["id"])
        raise DomainError(ErrorCode.INVALID_INPUT, "실행 기능을 찾을 수 없습니다.")
