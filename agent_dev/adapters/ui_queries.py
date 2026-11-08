"""Presentation queries expose application data; credentials never cross the boundary."""

import json
from pathlib import Path

from agent_dev.defaults import config_file
from agent_dev.domain.content import redact


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def snapshot(root):
    root = Path(root)
    from agent_dev.adapters.storage_registry import state_db_path

    database = state_db_path(root)
    runtime = _read(root / "config/runtime.json", {})
    status = _read(root / "data/agent_status.json", {})
    backlog = _read(root / "data/backlog.json", {"tasks": []})
    current = _read(root / "data/state.json", {})
    if (
        runtime.get("orchestrator_v2", {}).get("read")
        and database.exists()
    ):
        from agent_dev.adapters.state.sqlite.database import Database

        db = Database(database, read_only=True)
        tasks = db.rows("SELECT * FROM tasks")
        backlog = {
            "tasks": [
                dict(json.loads(row["definition_json"]), id=row["id"], status=row["state"])
                for row in tasks
            ]
        }
        priority = {
            "running": 0,
            "waiting_for_capacity": 1,
            "verifying": 2,
            "blocked": 3,
            "pr_ready": 4,
            "user_review": 5,
            "ready": 6,
            "approval_pending": 7,
            "completed": 8,
            "cancelled": 9,
        }
        active = min(tasks, key=lambda row: priority.get(row["state"], 10)) if tasks else None
        if active:
            phase = db.rows(
                "SELECT phase_id FROM phase_attempts WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (active["id"],),
            )
            mapping = {
                "waiting_for_capacity": "waiting_usage_limit",
                "verifying": "running",
                "completed": "idle",
                "ready": "idle",
                "approval_pending": "idle",
                "pr_ready": "idle",
                "user_review": "idle",
            }
            status = {
                "task_id": active["id"],
                "state": mapping.get(active["state"], active["state"]),
                "workflow_state": active["state"],
                "message": f"{active['title']}: {active['state']}",
                "phase": "verification"
                if active["state"] == "verifying"
                else phase[0]["phase_id"]
                if phase
                else "",
                "allowed_actions": ["resume", "checkpoint"]
                if active["state"] in {"blocked", "waiting_for_capacity"}
                else ["checkpoint"],
                "operator_actions": ["승인된 workflow 명령으로 작업을 진행합니다."],
                "updated_at": "",
            }
    from agent_dev.adapters.secrets import registered_secrets

    return redact(
        {
            "status": status,
            "backlog": backlog,
            "state": current,
            "runtime": runtime,
            "pipeline": _read(config_file("pipeline.json"), {"steps": []}),
        },
        registered_secrets(root),
    )


def settings(root):
    return snapshot(root)["runtime"]


def update_settings(root, source):
    from agent_dev.config.settings import preserve_masked
    from agent_dev.core.io import write_json

    root = Path(root)
    path = root / "config/runtime.json"
    current = _read(path, {})
    update = _read(Path(source), None)
    if not isinstance(update, dict):
        raise TypeError("Settings must be an object.")
    # Administrative migration flags require refactor configure and its gates.
    if update.get("orchestrator_v2", current.get("orchestrator_v2")) != current.get(
        "orchestrator_v2"
    ):
        raise ValueError("Migration flags require refactor configure.")
    write_json(path, preserve_masked(update, current))
    return {"updated": True}
