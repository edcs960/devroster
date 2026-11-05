"""Read-only, process-local signed continuations bound to a query and revision.

No persistence or GET writes. A server restart invalidates existing cursors;
clients must start a fresh query rather than silently treat it as exhausted.
Callers needing cross-process continuation pass their own private ``key``.
"""

import base64
import hashlib
import hmac
import json
import secrets

from agent_dev.domain.content import canonical, digest
from agent_dev.domain.errors import DomainError, ErrorCode

_KEY = secrets.token_bytes(32)


def encode(kind, scope, position, revision, key=None):
    raw = canonical(
        {"kind": kind, "scope": digest(scope), "position": position, "revision": revision}
    ).encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return payload + "." + hmac.new(key or _KEY, payload.encode(), hashlib.sha256).hexdigest()


def decode(value, kind, scope, revision, key=None):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ValueError
        payload, signature = value.split(".")
        expected = hmac.new(key or _KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        parsed = json.loads(raw)
        if parsed["kind"] != kind or parsed["scope"] != digest(scope):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise DomainError(
            ErrorCode.INVALID_INPUT,
            "이어보기 정보가 유효하지 않습니다. 조건을 확인하고 다시 조회해주세요.",
        ) from None
    if parsed["revision"] != revision:
        raise DomainError(
            ErrorCode.STALE_VERSION, "조회 이후 근거가 변경되었습니다. 처음부터 다시 조회해주세요."
        )
    return parsed["position"]
