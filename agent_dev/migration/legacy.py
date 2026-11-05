import json
from pathlib import Path

from agent_dev.adapters.state.sqlite.store import WorkflowRepository, validate_dag
from agent_dev.domain.content import content_digest, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.legacy import parse_task_status


def collect_sources(project_root: Path, data_dir: Path, secrets: tuple[str, ...] = ()) -> dict:
    """No writes, missing optional files allowed, malformed existing files rejected."""
    project_root, data_dir = project_root.resolve(), data_dir.resolve()
    sources = {}
    for name in ("backlog.json", "state.json"):
        path = data_dir / name
        if path.exists():
            try:
                sources[f"data/{name}"] = redact(json.loads(path.read_text()), secrets)
            except (ValueError, OSError):
                raise DomainError(
                    ErrorCode.INVALID_INPUT, "Invalid legacy metadata file."
                ) from None
    output = project_root / "output"
    if output.exists():
        for path in sorted(output.rglob("*")):
            if (
                not path.is_file()
                or path.is_symlink()
                or not path.resolve().is_relative_to(output.resolve())
            ):
                continue
            if path.name in {"pipeline-state.json", "events.jsonl"}:
                try:
                    raw = path.read_text()
                    value = (
                        [json.loads(line) for line in raw.splitlines() if line.strip()]
                        if path.suffix == ".jsonl"
                        else json.loads(raw)
                    )
                    sources[str(path.relative_to(project_root))] = redact(value, secrets)
                except (ValueError, OSError):
                    raise DomainError(
                        ErrorCode.INVALID_INPUT, "Invalid legacy run metadata."
                    ) from None
            elif path.suffix in {".md", ".png", ".json", ".html", ".zip"}:
                sources[str(path.relative_to(project_root))] = {
                    "artifact_digest": content_digest(path.read_bytes()),
                    "size": path.stat().st_size,
                }
    backlog = sources.get("data/backlog.json", {"tasks": []})
    if not isinstance(backlog, dict) or not isinstance(backlog.get("tasks"), list):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid backlog shape.")
    validate_dag(backlog["tasks"])
    for task in backlog["tasks"]:
        parse_task_status(task.get("status", "ready"))
    return sources


def projections(sources):
    records = {}
    for path, value in sources.items():
        if path.endswith("pipeline-state.json"):
            if not isinstance(value, dict) or not isinstance(value.get("phases", {}), dict):
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid pipeline state shape.")
            task_id = value.get("task_id", "")
            for phase, metadata in value.get("phases", {}).items():
                records[(path, "phase", phase)] = (task_id, metadata)
            for index, finding in enumerate(value.get("findings", [])):
                records[(path, "finding", str(index))] = (task_id, finding)
            if value.get("active_issue"):
                records[(path, "active_issue", "current")] = (task_id, value["active_issue"])
        elif path.endswith("events.jsonl"):
            if not isinstance(value, list):
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid event stream shape.")
            for index, event in enumerate(value):
                records[(path, "event", str(index))] = (event.get("task_id", ""), event)
        elif isinstance(value, dict) and "artifact_digest" in value:
            records[(path, "artifact", path)] = ("", value)
        elif path == "data/state.json":
            records[(path, "runtime", "current")] = ("", value)
    return records


def legacy_only(repository, sources):
    from agent_dev.core.output_paths import slugify

    prefixes = []
    for row in repository.db.rows(
        "SELECT id,project_id,definition_json FROM tasks WHERE plan_id IS NOT NULL"
    ):
        definition = json.loads(row["definition_json"])
        prefixes.append(
            f"output/{slugify(definition.get('system_name') or row['project_id'])}/{row['id']}/"
        )
    return {
        path: value
        for path, value in sources.items()
        if not any(path.startswith(prefix) for prefix in prefixes)
    }


def compare(repository: WorkflowRepository, sources: dict) -> dict:
    sources = legacy_only(repository, sources)
    stored = legacy_only(
        repository,
        {
            row["path"]: json.loads(row["content_json"])
            for row in repository.db.rows("SELECT * FROM legacy_sources")
        },
    )
    differences = [
        {
            "path": key,
            "reason": "missing"
            if key not in stored
            else "removed"
            if key not in sources
            else "changed",
        }
        for key in sorted(sources.keys() | stored.keys())
        if sources.get(key) != stored.get(key)
    ]
    # Validate normalized task and dependency projections too, not just raw source backups.
    tasks = sources.get("data/backlog.json", {}).get("tasks", [])
    projected = {
        row["id"]: row for row in repository.db.rows("SELECT * FROM tasks WHERE plan_id IS NULL")
    }
    for task in tasks:
        row = projected.get(task["id"])
        if (
            row is None
            or json.loads(row["definition_json"]) != task
            or row["state"] != parse_task_status(task.get("status", "ready")).value
        ):
            differences.append({"path": task["id"], "reason": "task_projection"})
    expected = {(t["id"], d) for t in tasks for d in t.get("depends_on", [])}
    imported_ids = set(projected)
    actual = {
        (r["task_id"], r["depends_on_task_id"])
        for r in repository.db.rows("SELECT * FROM task_dependencies")
        if r["task_id"] in imported_ids
    }
    if actual != expected:
        differences.append({"path": "task_dependencies", "reason": "edge_projection"})
    expected_projection = projections(sources)
    actual_projection = {
        (r["source_path"], r["kind"], r["record_id"]): (r["task_id"], json.loads(r["content_json"]))
        for r in repository.db.rows("SELECT * FROM legacy_projections")
    }
    for key in expected_projection.keys() | actual_projection.keys():
        if expected_projection.get(key) != actual_projection.get(key):
            differences.append({"path": key[0], "reason": key[1] + "_projection"})
    if set(projected) != {task["id"] for task in tasks}:
        differences.append({"path": "tasks", "reason": "task_set_projection"})
    return {
        "zero_diff": not differences,
        "source_count": len(sources),
        "task_count": len(tasks),
        "differences": differences,
    }


def import_sources(
    repository: WorkflowRepository | None, sources: dict, *, dry_run: bool = True
) -> dict:
    if repository is not None:
        sources = legacy_only(repository, sources)
    tasks = sources.get("data/backlog.json", {}).get("tasks", [])
    validate_dag(tasks)
    report = {
        "dry_run": dry_run,
        "source_count": len(sources),
        "task_count": len(tasks),
        "manifest": [
            {"path": path, "digest": digest(value)} for path, value in sorted(sources.items())
        ],
    }
    if dry_run:
        return report
    if repository is None:
        raise DomainError(ErrorCode.INVALID_INPUT, "Repository required for actual import.")
    with repository.db.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO projects VALUES('legacy','Legacy','Imported','draft',0,?)",
            (repository.clock(),),
        )
        for task in tasks:
            existing = connection.execute(
                "SELECT plan_id FROM tasks WHERE id=?", (task["id"],)
            ).fetchone()
            if existing and existing[0] is not None:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Legacy import conflicts with a planned task."
                )
            connection.execute(
                "INSERT INTO tasks(id,project_id,title,priority,state,definition_json) VALUES(?,'legacy',?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title,priority=excluded.priority,state=excluded.state,definition_json=excluded.definition_json",
                (
                    task["id"],
                    task["title"],
                    task.get("priority", "medium"),
                    parse_task_status(task.get("status", "ready")).value,
                    repository._json(task),
                ),
            )
        for task in tasks:
            connection.execute("DELETE FROM task_dependencies WHERE task_id=?", (task["id"],))
            for dependency in task.get("depends_on", []):
                connection.execute(
                    "INSERT INTO task_dependencies VALUES(?,?)", (task["id"], dependency)
                )
        for path, value in sources.items():
            connection.execute(
                "INSERT INTO legacy_sources VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET digest=excluded.digest,content_json=excluded.content_json",
                (path, digest(value), repository._json(value)),
            )
        for (path, kind, record_id), (task_id, value) in projections(sources).items():
            connection.execute(
                "INSERT INTO legacy_projections VALUES(?,?,?,?,?) ON CONFLICT(source_path,kind,record_id) "
                "DO UPDATE SET task_id=excluded.task_id,content_json=excluded.content_json",
                (path, kind, record_id, task_id, repository._json(value)),
            )
    report["comparison"] = compare(repository, sources)
    return report
