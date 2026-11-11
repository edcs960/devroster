"""Read-only usage, cost and progress dashboard for the open workspace."""

from typing import ClassVar
from urllib.parse import urlsplit

from agent_dev.adapters import usage_dashboard
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.local_web import LocalWeb


class UsageWeb(LocalWeb):
    prefix = page = "usage"
    get_routes: ClassVar = {"/api/usage/bootstrap", "/api/usage/summary"}

    def handle(self, handler):
        path = urlsplit(handler.path).path
        if path.startswith("/api/") and (handler.command != "GET" or path not in self.get_routes):
            from agent_dev.ui.studio_server import StudioWeb

            StudioWeb.send(handler, {"error": "요청 경로를 확인해주세요."}, status=400)
            return
        super().handle(handler)

    def get(self, action, query):
        if set(query) - {"days"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "조회 조건을 확인해주세요.")
        days = query.get("days", "30")
        if not days.isdigit() or len(days) > 3:
            raise DomainError(ErrorCode.INVALID_INPUT, "기간은 7·30·90일 또는 전체로 선택해주세요.")
        if action in {"bootstrap", "summary"}:
            return usage_dashboard.summary(self.root, int(days))
        raise DomainError(ErrorCode.INVALID_INPUT, "조회 기능을 찾을 수 없습니다.")
