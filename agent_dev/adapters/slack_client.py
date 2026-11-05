"""Minimal Slack Web API client over the standard library (no proxy, no redirects)."""

import json
import urllib.error
import urllib.parse
import urllib.request

from agent_dev.domain.errors import DomainError, ErrorCode

BASE = "https://slack.com/api/"
LIMIT = 2_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SlackClient:
    def __init__(self, token, *, base=BASE, opener=None, timeout=15):
        if not isinstance(token, str) or not token.startswith(("xoxb-", "xoxp-")):
            raise DomainError(
                ErrorCode.AUTH_REQUIRED, "슬랙 봇 토큰(xoxb-로 시작)을 먼저 저장해주세요."
            )
        self.token, self.base, self.timeout = token, base, timeout
        # The token must never reach an ambient proxy or a redirect target.
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def call(self, method, **params):
        data = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        ).encode()
        request = urllib.request.Request(
            self.base + method,
            data=data,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(LIMIT + 1)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 429:
                raise DomainError(
                    ErrorCode.EXTERNAL_TRANSIENT, "슬랙 요청 한도에 걸렸습니다. 잠시 뒤 다시 시도합니다."
                ) from None
            raise DomainError(
                ErrorCode.EXTERNAL_TRANSIENT, f"슬랙 API 요청 실패(HTTP {code})."
            ) from None
        except (OSError, urllib.error.URLError):
            raise DomainError(ErrorCode.EXTERNAL_TRANSIENT, "슬랙에 연결하지 못했습니다.") from None
        if len(body) > LIMIT:
            raise DomainError(ErrorCode.INVALID_INPUT, "슬랙 응답이 너무 큽니다.")
        try:
            payload = json.loads(body)
        except ValueError:
            raise DomainError(ErrorCode.INVALID_INPUT, "슬랙 응답 형식이 올바르지 않습니다.") from None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            reason = payload.get("error", "unknown") if isinstance(payload, dict) else "unknown"
            code = (
                ErrorCode.AUTH_REQUIRED
                if reason in {"invalid_auth", "not_authed", "account_inactive", "token_revoked"}
                else ErrorCode.CAPABILITY_UNAVAILABLE
            )
            raise DomainError(code, f"슬랙 API 오류: {reason}")
        return payload

    def identity(self):
        return self.call("auth.test")

    def channel(self, channel_id):
        return self.call("conversations.info", channel=channel_id)["channel"]

    def history(self, channel_id, oldest, limit=50):
        """Messages newer than ``oldest``, oldest first."""
        rows = self.call(
            "conversations.history", channel=channel_id, oldest=oldest, limit=limit, inclusive="false"
        ).get("messages", [])
        return sorted(rows, key=lambda row: float(row.get("ts", "0")))

    def post(self, channel_id, text, thread_ts=None):
        return self.call(
            "chat.postMessage",
            channel=channel_id,
            text=text[:3500],
            thread_ts=thread_ts,
            unfurl_links="false",
            unfurl_media="false",
        )
