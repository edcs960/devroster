"""Acceptance criteria and the tests that check them. Pure rules, no I/O.

Test Automation names one test per acceptance criterion
(``{"REQ-001:AC-01": "tests/test_file.py::test_name"}``). The same map feeds three
readers: the links made after verification passes, the proof of a ``test`` link, and the
drift reviewer.
"""

from __future__ import annotations

from typing import Any


def attach(requirements, named) -> list[dict[str, Any]]:
    """Each acceptance criterion with the test named for it ("" when none)."""
    named = named if isinstance(named, dict) else {}

    def selector(criterion):
        value = named.get(criterion)
        return value.strip()[:300] if isinstance(value, str) else ""

    return [dict(r, acceptance=[dict(c, test=selector(c["id"])) for c in r["acceptance"]]) for r in requirements]


def coverage(requirements):
    """How many acceptance criteria have a named test, and which do not."""
    criteria = [c for r in requirements for c in r.get("acceptance", [])]
    unnamed = [c["id"] for c in criteria if not c.get("test")]
    return {"total": len(criteria), "named": len(criteria) - len(unnamed), "unnamed": unnamed}


def grouped(named, requirement_ids):
    """(requirement, test file) -> the criteria that file covers, for this task's requirements.

    A key like "REQ-001:*0" marks a changed test without a known criterion: the file is
    kept and its criteria stay empty.
    """
    result: dict[tuple[str, str], set[str]] = {}
    for criterion, selector in named.items():
        if not isinstance(criterion, str) or ":" not in criterion or not isinstance(selector, str):
            continue
        requirement, _, name = criterion.partition(":")
        if requirement not in requirement_ids:
            continue
        path = selector.split("::", 1)[0].strip()
        if path:
            criteria = result.setdefault((requirement, path), set())
            if not name.startswith("*"):
                criteria.add(criterion)
    return result


def mappings(link, definition):
    """The approved plan's verification mappings that cover this link's target and criteria."""
    return [
        m
        for m in definition.get("verification_mapping", [])
        if m["requirement_id"] == link["requirement_id"]
        and m["target"] == link["target_ref"]
        and set(link.get("criteria_ids") or []).issubset(m["criteria_ids"])
    ]


def registered_commands(link, commands, gates):
    """The registered verification commands of the link's module that run its target in ``gates``."""
    return [
        c
        for c in commands
        if c.get("repository") == link.get("module_id") and c["name"] in gates and link["target_ref"] in c.get("command", [])
    ]
