"""External messenger page: Slack settings, bot token, connection check and the listener."""

from typing import ClassVar
from urllib.parse import urlsplit

from agent_dev.adapters import messenger
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.local_web import LocalWeb


class MessengerWeb(LocalWeb):
    prefix = page = "messengers"
    get_routes: ClassVar = {"/api/messengers/bootstrap"}
    post_routes: ClassVar = {
        "/api/messengers/save",
        "/api/messengers/token",
        "/api/messengers/app-token",
        "/api/messengers/test",
        "/api/messengers/listener",
    }

    def __init__(self, root):
        super().__init__(root)
        self.listener = None

    def _listener(self):
        if self.listener is None:
            self.listener = messenger.MessengerListener(self.root)
        return self.listener

    def autostart(self):
        """The app starts the listener when the saved settings say so."""
        settings = messenger.read_settings(self.root)
        if settings["enabled"] and messenger.token_saved():
            self._listener().start()

    def close(self):
        if self.listener is not None:
            self.listener.stop()

    def handle(self, handler):
        path = urlsplit(handler.path).path
        allowed = self.get_routes if handler.command == "GET" else self.post_routes
        if path.startswith("/api/") and path not in allowed:
            from agent_dev.ui.responses import send

            send(handler, {"error": "요청 경로를 확인해주세요."}, status=400)
            return
        super().handle(handler)

    def get(self, action, query):
        return messenger.status(self.root, self.listener)

    def post(self, action, payload):
        if action == "save":
            before = messenger.read_settings(self.root)
            saved = messenger.save_settings(self.root, payload.get("settings"))
            if not saved["enabled"] and self.listener is not None:
                self.listener.stop()
            elif self.listener is not None and self.listener.running and any(
                before[key] != saved[key] for key in ("socket_mode", "channel_id")
            ):
                self.listener.stop()  # The Socket Mode connection is made when it starts.
                self.listener.start()
        elif action == "token":
            if payload.get("delete") is True:
                messenger.delete_token()
            else:
                messenger.save_token(payload.get("value"))
        elif action == "app-token":
            if payload.get("delete") is True:
                messenger.delete_app_token()
            else:
                messenger.save_app_token(payload.get("value"))
        elif action == "test":
            return {"check": messenger.check(self.root), **messenger.status(self.root, self.listener)}
        else:
            operation = payload.get("action")
            if operation == "start":
                settings = messenger.read_settings(self.root)
                if not settings["enabled"]:
                    raise DomainError(ErrorCode.INVALID_INPUT, "설정에서 '사용'을 켜고 저장한 뒤 시작해주세요.")
                if not messenger.token_saved():
                    raise DomainError(ErrorCode.AUTH_REQUIRED, "슬랙 봇 토큰을 먼저 저장해주세요.")
                self._listener().start()
            elif operation == "stop":
                if self.listener is not None:
                    self.listener.stop()
            else:
                raise DomainError(ErrorCode.INVALID_INPUT, "시작 또는 중지를 선택해주세요.")
        return messenger.status(self.root, self.listener)
