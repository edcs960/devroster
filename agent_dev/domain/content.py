"""Canonical serialization and secret-safe bounded content, without environment access."""

import hashlib
import json
import re
from typing import Any

# Values under matching keys are stored as "****" in events and artifacts. Name non-secret
# fields without these words (e.g. `resume_session`, not `resume_token`).
_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|authorization|private[_-]?key)", re.IGNORECASE
)
_TOKEN_START = r"(?:(?<![A-Za-z0-9])|(?<=\\[nrtfvb])|(?<=%[0-9A-Fa-f]{2})|(?<=\\u[0-9A-Fa-f]{4}))"
_SECRET_TEXT = re.compile(
    # Token prefixes must start a word so "task-verify-…" or "risk-assessment-…" stay intact,
    # while escaped (\n, \t, \u0022) and URL-encoded (%3D, %22) neighbours still count as a start.
    r"(?:" + _TOKEN_START + r"sk-[A-Za-z0-9_-]{12,}|" + _TOKEN_START + r"gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----)"
)


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(k): (
                "****"
                if _SECRET_KEY.search(str(k))
                and isinstance(v, (str, dict, list))
                and v
                and not (str(k) == "token_mode" and isinstance(v, str) and v in {"off", "balanced", "aggressive"})
                and not (str(k) == "token_measurement" and isinstance(v, str) and v in {"estimated", "actual"})
                and not str(k).endswith(("_ref", "_refs"))
                else redact(v, secrets)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, "****")
        return _SECRET_TEXT.sub("****", value)
    return value


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def content_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
