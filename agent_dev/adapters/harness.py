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
}


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def parse_harness(output: str, allowed_phases: set[str] | None = None) -> dict:
    if not isinstance(output, str) or len(output) > 2_000_000:
        raise DomainError(ErrorCode.INVALID_INPUT, "Agent output exceeds control parser limits.")
    controls = []
    for block in re.findall(r"```(?:json)?\s*(.*?)\s*```", output, re.DOTALL):
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
