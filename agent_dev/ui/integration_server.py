"""Optional project integration catalog and setup API."""

from agent_dev.adapters.integrations import Integrations
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.local_web import LocalWeb


class IntegrationWeb(LocalWeb):
    prefix = page = "integrations"

    def __init__(self, root):
        super().__init__(root)
        self.service = Integrations(root)

    def get(self, action, query):
        if action == "bootstrap":
            return {
                "catalog": self.service.catalog(),
                "connections": self._connections(),
                "project": self.service.project,
            }
        if action == "launch":
            return self.service.launch(query["id"])
        if action == "catalog":
            return self.service.catalog()
        if action == "list":
            return self.service.list()
        if action == "detail":
            return self.service.show(query["id"])
        if action == "artifact":
            return self.service.cached(query["id"], query["digest"])
        if action == "export":
            return self.service.export()
        if action == "callback":
            return self.service.oauth.complete(query.get("state", ""), query.get("code", ""), self.service.show)
        return super().get(action, query)

    def _connections(self):
        """Rows for the page: a stdio row also says whether its launch command was confirmed."""
        rows = self.service.list()
        for row in rows:
            if row.get("transport") == "stdio" and row.get("command"):
                row["launch_approved"] = self.service.launch(row)["approved"]
        return rows

    def post(self, action, payload):
        service = self.service
        if action == "approve-launch":
            return service.approve_launch(payload["id"], payload["digest"])
        if action == "configure":
            return service.configure(payload["config"], payload.get("expected_version", 0))
        if action == "check":
            return service.check(
                payload["id"],
                operation=payload.get("operation", ""),
                arguments=payload.get("arguments", {}),
                scope=payload.get("scope", ""),
            )
        if action == "disable":
            return service.disable(payload["id"])
        if action == "import":
            return service.import_document(payload["document"])
        if action == "auth":
            return service.oauth.start(service.show(payload["id"]), payload["redirect_uri"])
        if action == "prepare":
            from agent_dev.adapters.integration_catalog import provider

            row = provider(payload["provider"])
            return {
                "provider": row["id"],
                "official_documentation": row["documentation"],
                "command": row["command"],
                "automatic_install": False,
                "message": "공식 설치 안내에서 버전을 고정하여 설치한 뒤 실행 파일을 등록해주세요.",
            }
        if action == "read":
            return service.call(
                payload["id"],
                payload["operation"],
                payload.get("arguments", {}),
                scope=payload["scope"],
            )
        raise DomainError(ErrorCode.INVALID_INPUT, "기능을 찾을 수 없습니다.")
