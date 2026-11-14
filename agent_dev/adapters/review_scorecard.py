"""Read model for the reviewer scorecard: decided findings from the Studio DB and drift events.

Spec review findings live in the Studio DB (``findings``), drift findings and the decisions
on them are task events in the workspace DB. Both are read as they are; the counting is
domain.review_scorecard. Read-only.
"""

from __future__ import annotations

import json

from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.domain import drift, review_scorecard, spec_review
from agent_dev.domain.role_policy import independent_family


def spec_rows(path, since=None):
    """Rows of spec review findings created in the period; empty without a Studio DB."""
    if not path.is_file():
        return []
    studio = Database(path, read_only=True)
    if not studio.rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name='findings'"):
        return []  # A Studio DB from before independent reviews.
    findings = []
    for row in studio.rows(
        "SELECT lens,state,reviewers FROM findings WHERE created>=? ORDER BY created,rowid", (since or 0,)
    ):
        try:
            reviewers = json.loads(row["reviewers"])
        except ValueError:
            continue
        findings.append({"lens": row["lens"], "state": row["state"], "reviewers": reviewers})
    return review_scorecard.spec_rows(findings)


def drift_rows(db, since=None):
    """Rows of drift findings from checks in the period.

    A task is checked again after every change, so an earlier check's findings count only
    when the owner decided them; the latest check's findings all count.
    """
    tasks = {}
    for row in db.rows(
        "SELECT aggregate_id,kind,payload_json,created_at FROM events "
        "WHERE kind IN ('DriftChecked','DriftFindingProcessed') ORDER BY aggregate_id,version"
    ):
        try:
            payload = json.loads(row["payload_json"])
        except ValueError:
            continue
        if isinstance(payload, dict):
            tasks.setdefault(row["aggregate_id"], {"DriftChecked": [], "DriftFindingProcessed": []})[row["kind"]].append(
                dict(payload, _at=row["created_at"])
            )
    rows = []
    for events in tasks.values():
        checks = [c for c in events["DriftChecked"] if c.get("id")]
        if not checks:
            continue
        reviewers = {c["id"]: c for c in checks}
        for finding in drift.decided(checks, events["DriftFindingProcessed"]):
            check = reviewers[finding["check_id"]]
            if check["_at"] < (since or 0) or (finding["state"] == "open" and check is not checks[-1]):
                continue
            provider = check.get("provider") or ""
            rows.append(
                {
                    "source": "drift",
                    "provider": provider,
                    "model": check.get("model") or "",
                    "family": independent_family(provider),
                    "group": finding.get("category") or "",
                    "state": finding["state"],
                }
            )
    return rows


def summary(db, studio_path, since=None):
    rows = spec_rows(studio_path, since) + drift_rows(db, since)
    return {
        "rows": review_scorecard.scorecard(rows),
        "min_decisions": review_scorecard.MIN_DECISIONS,
        "sources": review_scorecard.SOURCES,
        "groups": {
            "spec": {lens: value["label"] for lens, value in spec_review.LENSES.items()},
            "drift": drift.CATEGORIES,
        },
    }
