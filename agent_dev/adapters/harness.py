"""Untrusted agent output must satisfy a bounded, explicit control schema."""

import json
import re

from agent_dev.domain.errors import DomainError, ErrorCode

_FIELDS = {
    "handoff_status",
    # Session nodes list the files they changed so each change is checked against its role.
    "changed_paths",
    "issue_type",
    "owning_agent_id",
    "root_cause",
    "reentry_step_id",
    "summary",
    # Code roles write the commit message in the project's language (see output_language).
    "commit_message",
}


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _trailing_object(output):
    """A control object written without a code fence at the very end of the output.

    Smaller models often drop the fence. Only an object that ends the output counts, so a
    JSON example quoted earlier in the text is never taken as the result.
    """
    text = output.rstrip()
    if not text.endswith("}"):
        return None
    window = len(text) - min(len(text), 20000)
    start = text.rfind("{", window)
    while start >= window:
        candidate = text[start:]
        if "handoff_status" in candidate:
            try:
                json.loads(candidate)
            except ValueError:
                pass
            else:
                return candidate
        start = text.rfind("{", window, start)
    return None


def parse_harness(output: str, allowed_phases: set[str] | None = None) -> dict:
    if not isinstance(output, str) or len(output) > 2_000_000:
        raise DomainError(ErrorCode.INVALID_INPUT, "Agent output exceeds control parser limits.")
    controls = []
    blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", output, re.DOTALL)
    if not any("handoff_status" in block for block in blocks):
        trailing = _trailing_object(output)
        if trailing:
            blocks = [trailing]
    for block in blocks:
        if "handoff_status" not in block:
            continue
        try:
            payload = json.loads(block, object_pairs_hook=_unique_fields)
        except ValueError:
            raise DomainError(ErrorCode.INVALID_INPUT, "Malformed Harness Control JSON.") from None
        if not isinstance(payload, dict) or set(payload) - _FIELDS:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown Harness Control field.")
        if any(not isinstance(value, str) or len(value) > 12000 for value in payload.values()):
            raise DomainError(ErrorCode.INVALID_INPUT, "Harness fields must be bounded strings.")
        if payload.get("handoff_status") not in {"pass", "issue", "blocked", "rework"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid Harness status.")
        if (
            payload["handoff_status"] != "pass"
            and not {"issue_type", "root_cause", "reentry_step_id"} <= payload.keys()
        ):
            raise DomainError(ErrorCode.INVALID_INPUT, "Issue control fields missing.")
        owner = payload.get("reentry_step_id", "")
        if owner and allowed_phases is not None and owner not in allowed_phases:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Unknown reentry phase.")
        if payload["handoff_status"] == "pass" and owner:
            raise DomainError(ErrorCode.INVALID_INPUT, "Pass result cannot request reentry.")
        controls.append(payload)
    if len(controls) != 1:
        raise DomainError(ErrorCode.INVALID_INPUT, "Exactly one Harness Control block required.")
    return controls[0]
