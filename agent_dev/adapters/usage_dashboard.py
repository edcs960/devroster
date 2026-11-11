"""Read model for the usage and progress dashboard (/usage).

Reads tasks, RunnerUsage events and each task's approved usage_policy from the
workspace state DB, and Studio job.usage events from the Studio DB, both read-only;
the figures come from domain.usage_report. A Studio project is linked to the tasks
whose plan one of its handoffs created (handoff_imports.plan_id = tasks.plan_id).
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

from agent_dev.adapters.execution_view import STATE_LABELS, task_policy
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.usage_policy import normalize_policy
from agent_dev.domain.usage_report import report

PERIODS = (7, 30, 90, 0)  # Days; 0 is the whole history.
CHART_DAYS = 90  # The daily series never covers more than this many days.


def local_day(timestamp):
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def day_labels(first, now, days):
    """Local calendar days ending today: the period, or since the first call; at most CHART_DAYS."""
    today = date.fromtimestamp(now)
    count = days or ((today - date.fromtimestamp(first)).days + 1 if first else 1)
    count = max(1, min(count, CHART_DAYS))
    return [(today - timedelta(days=offset)).isoformat() for offset in range(count - 1, -1, -1)]


# Studio calls carry the cost computed with the price book frozen when the job was queued.
# Only rows recorded before that existed (no cost_reason) fall back to the current price book.
STUDIO_POLICY = "studio"


def current_policy(db):
    """usage_policy of the latest saved project profile; Studio jobs have no snapshot."""
    row = db.rows(
        "SELECT content_json FROM project_profile_versions ORDER BY rowid DESC LIMIT 1"
    )
    if not row:
        return None
    try:
        return normalize_policy(json.loads(row[0]["content_json"]).get("usage_policy"))
    except (DomainError, ValueError, AttributeError):
        return None


def studio_usage(path, since):
    """(calls, {studio id: title}) from the Studio DB; empty when there is none yet."""
    if not path.is_file():
        return [], {}
    studio = Database(path, read_only=True)
    titles = {row["id"]: row["title"] for row in studio.rows("SELECT id,title FROM projects")}
    calls = []
    for row in studio.rows(
        "SELECT id,project_id,payload,created FROM events WHERE kind='job.usage' AND created>=? ORDER BY created,id",
        (since or 0,),
    ):
        try:
            data = json.loads(row["payload"])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        priced = (
            {"cost": data.get("cost"), "currency": data.get("currency"), "reason": data.get("cost_reason")}
            if "cost_reason" in data
            else None
        )
        calls.append(
            {
                "studio_id": row["project_id"],
                "policy_key": STUDIO_POLICY,
                "priced": priced,
                "created_at": row["created"],
                "payload": {
                    "attempt_id": f"studio-{row['id']}",
                    "phase_id": "studio:" + str(data.get("step") or ""),
                    "provider": data.get("provider") or "",
                    "usage": data.get("usage"),
                    "selection": {"model": data.get("model") or ""},
                    "runner_seconds": data.get("runner_seconds"),
                },
            }
        )
    return calls, titles


def handoff_links(db):
    links = {}
    if not db.rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name='handoff_imports'"):
        return links  # A workspace from before Studio handoffs.
    for row in db.rows(
        "SELECT DISTINCT h.source_studio_id AS studio_id,t.id AS task_id FROM handoff_imports h "
        "JOIN tasks t ON t.plan_id=h.plan_id ORDER BY t.id"
    ):
        links.setdefault(row["studio_id"], []).append(row["task_id"])
    return links


def summary(root, days=30, now=None):
    if days not in PERIODS:
        raise DomainError(ErrorCode.INVALID_INPUT, "기간은 7·30·90일 또는 전체로 선택해주세요.")
    now = time.time() if now is None else now
    queries = TaskQueries(root)
    if queries.db is None:
        return {"ready": False, "days": days, "periods": list(PERIODS)}
    db = queries.db
    tasks = [
        dict(row)
        for row in db.rows(
            "SELECT t.id,t.title,t.state,MAX(e.created_at) AS updated_at FROM tasks t "
            "LEFT JOIN events e ON e.aggregate_id=t.id GROUP BY t.id ORDER BY t.id"
        )
    ]
    # A period starts at local midnight of its first day, so totals match the daily series.
    labels = day_labels(None, now, days) if days else None
    since = datetime.combine(date.fromisoformat(labels[0]), dtime.min).timestamp() if labels else None
    calls = [
        {"task_id": row["aggregate_id"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]}
        for row in db.rows(
            "SELECT aggregate_id,payload_json,created_at FROM events WHERE kind='RunnerUsage' "
            "AND created_at>=? ORDER BY created_at,version",
            (since or 0,),
        )
    ]
    repository = WorkflowRepository(db)
    policies = {task_id: task_policy(repository, task_id) for task_id in {c["task_id"] for c in calls}}
    planning, studio_titles = studio_usage(queries.locations.studio_db, since)
    if any(c["priced"] is None for c in planning):
        policies[STUDIO_POLICY] = current_policy(db)
    links = handoff_links(db)
    studios = {
        studio_id: {"title": studio_titles.get(studio_id, studio_id), "tasks": links.get(studio_id, [])}
        for studio_id in set(studio_titles) | set(links)
    }
    calls = sorted(calls + planning, key=lambda c: c["created_at"])
    if labels is None:
        labels = day_labels(calls[0]["created_at"] if calls else None, now, days)
    result = report(tasks, calls, policies, since=since, day=local_day, days=labels, studios=studios)
    result.update(
        ready=True,
        days=days,
        periods=list(PERIODS),
        generated_at=now,
        state_labels=STATE_LABELS,
        studio_note=(
            "Studio 생성 사용량은 기록을 시작한 버전부터 포함하며, 생성을 시작할 때의 프로젝트 가격표로 계산합니다."
        ),
    )
    return redact(result, queries.secrets)
