"""Approved limits and conservative usage projections, without provider pricing."""

import math

from agent_dev.domain.errors import DomainError, ErrorCode

DEFAULT_LIMITS = {
    "max_attempts": 20,
    "max_runner_seconds": 7200,
    "phase_timeout_seconds": 1800,
    "max_reentries": 1,
    # One session invocation (ADR 0002); 0 keeps the sum of its member role timeouts.
    "session_timeout_seconds": 0,
}
BOUNDS = {
    "max_attempts": (1, 1000),
    "max_runner_seconds": (1, 86400),
    "phase_timeout_seconds": (1, 7200),
    "max_reentries": (0, 20),
    "session_timeout_seconds": (0, 86400),
}


def limits(value=None):
    if value is not None and (not isinstance(value, dict) or set(value) - set(BOUNDS)):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid execution limit keys.")
    result = dict(DEFAULT_LIMITS, **(value or {}))
    for key, item in result.items():
        low, high = BOUNDS[key]
        if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
            raise DomainError(
                ErrorCode.INVALID_INPUT,
                f"Execution limit {key} must be an integer between {low} and {high}.",
            )
    return result


def safe_usage(value):
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if key in {"input_tokens", "output_tokens", "cached_input_tokens"}
        and isinstance(item, int)
        and not isinstance(item, bool)
        and 0 <= item <= 10**12
    }


def summarize(attempts, events, now):
    measured = {}
    for item in events:
        if item.get("attempt_id"):
            measured[item["attempt_id"]] = item
    seconds = 0.0
    tokens = {key: 0 for key in ["input_tokens", "output_tokens", "cached_input_tokens"]}
    known = dict.fromkeys(tokens, 0)
    for attempt in attempts:
        item = measured.get(attempt["id"], {})
        elapsed = item.get("runner_seconds")
        if not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or elapsed < 0:
            elapsed = max(0, (attempt.get("ended_at") or now) - attempt["started_at"])
        seconds += elapsed
        usage = safe_usage(item.get("usage"))
        for key, count in usage.items():
            tokens[key] += count
            known[key] += 1
    return {
        "attempts": len(attempts),
        "runner_seconds": round(seconds, 3),
        "measurements": [
            {
                "metric": key,
                "value": tokens[key] if known[key] else None,
                "known_attempts": known[key],
                "unknown_attempts": len(attempts) - known[key],
                "complete": bool(attempts) and known[key] == len(attempts),
            }
            for key in tokens
        ],
    }
