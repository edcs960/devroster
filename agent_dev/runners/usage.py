"""Parse only bounded usage fields from provider machine-readable results."""

import json

from agent_dev.domain.execution_limits import safe_usage


def codex_usage(stdout):
    totals = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            for key, count in safe_usage(event.get("usage")).items():
                totals[key] = totals.get(key, 0) + count
    return totals


def codex_session(stdout):
    """Thread id from a Codex ``exec --json`` event stream, or an empty string."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            value = event.get("thread_id")
            return value if isinstance(value, str) and len(value) <= 100 else ""
    return ""


def claude_session(stdout):
    """Session id from a Claude ``--output-format json`` result, or an empty string."""
    try:
        result = json.loads(stdout)
    except ValueError:
        return ""
    value = result.get("session_id") if isinstance(result, dict) else None
    return value if isinstance(value, str) and len(value) <= 100 else ""


def claude_result(stdout):
    try:
        result = json.loads(stdout)
    except ValueError:
        return stdout, {}
    if not isinstance(result, dict):
        return stdout, {}
    return str(result.get("result", "")), safe_usage(result.get("usage"))
