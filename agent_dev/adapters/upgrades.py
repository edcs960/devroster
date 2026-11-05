"""Idle workspace upgrades with preflight, backup and recoverable config journal."""

import base64
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import NoReturn

from agent_dev.adapters.backup import create_bundle, validate_database
from agent_dev.adapters.secrets import registered_secrets
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.workspace import configuration_root, metadata
from agent_dev.domain.content import canonical, content_digest, digest
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.execution_limits import limits

TARGET = "0.3.0"
FILES = ("workspace.json", "config/profile.json")


def reject(message) -> NoReturn:
    raise DomainError(ErrorCode.POLICY_VIOLATION, message)


def idle(sql):
    for query, args in [
        ("SELECT 1 FROM leases WHERE expires_at>? LIMIT 1", (time.time(),)),
        ("SELECT 1 FROM tasks WHERE state IN ('running','verifying') LIMIT 1", ()),
        ("SELECT 1 FROM commands WHERE state='running' LIMIT 1", ()),
        ("SELECT 1 FROM phase_attempts WHERE state='running' LIMIT 1", ()),
    ]:
        if sql.execute(query, args).fetchone():
            reject("Workspace is active; finish or recover execution before upgrade.")


def atomic(path, payload):
    temporary = path.with_name(path.name + ".upgrade-tmp")
    # No following symlinks and no replacing another owner's staging file.
    with temporary.open("xb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def inputs(root):
    values = {}
    for name in FILES:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            reject("Upgrade requires regular managed configuration files.")
        values[name] = path.read_bytes()
    return values


def preflight(root):
    root = configuration_root(root)
    marker = metadata(root)
    if not marker:
        reject("Upgrade supports initialized portable workspaces only.")
    current = marker.get("engine_version", "0.2.0")
    if current not in {"0.2.0", TARGET}:
        reject("Unsupported or newer workspace version; use a compatible package.")
    validate_database(root / "state.db")
    db = Database(root / "state.db", read_only=True)
    sql = db.connect()
    try:
        idle(sql)
    finally:
        sql.close()
    originals = inputs(root)
    profile = json.loads(originals["config/profile.json"])
    limits(profile.get("execution_limits"))
    for row in db.rows("SELECT content_json FROM project_profile_versions"):
        limits(json.loads(row["content_json"]).get("execution_limits"))
    return {
        "from_version": current,
        "to_version": TARGET,
        "schema_version": db.rows("SELECT MAX(version) AS v FROM schema_versions")[0]['v'],
        "changes": []
        if current == TARGET
        else [
            "Record workspace engine version",
            "Add bounded execution defaults to latest profiles (historical snapshots unchanged)",
        ],
        "pending_recovery": (root / "upgrade-journal.json").exists(),
        "operator_actions": [
            "Run devroster upgrade --apply to back up and apply; use --recover for an interrupted journal."
        ],
    }


def fingerprint(sql):
    return content_digest("\n".join(sql.iterdump()).encode())


def recover(root):
    root = configuration_root(root)
    journal = root / "upgrade-journal.json"
    if not journal.is_file() or journal.is_symlink():
        reject("No valid upgrade journal to recover.")
    value = json.loads(journal.read_text())
    if value.get("format_version") != 1 or set(value.get("files", {})) != set(FILES):
        reject("Invalid upgrade journal.")
    # Validate the schema without migrating it, then hold the write lock on a plain
    # connection: a mode=ro connection cannot take BEGIN IMMEDIATE. Recovery only reads.
    db = Database(root / "state.db", read_only=True)
    sql = sqlite3.connect(db.path, timeout=10, isolation_level=None)
    sql.row_factory = sqlite3.Row
    sql.execute("PRAGMA busy_timeout=10000")
    try:
        sql.execute("BEGIN IMMEDIATE")
        idle(sql)
        committed = any(
            json.loads(row[0]).get("upgrade_id") == value["id"]
            for row in sql.execute("SELECT payload_json FROM events WHERE kind='WorkspaceUpgraded'")
        )
        originals = inputs(root)
        # Recover only config still owned by this interrupted operation.
        for name, record in value["files"].items():
            if content_digest(originals[name]) not in {record["old_digest"], record["new_digest"]}:
                reject("Configuration changed after interruption; recovery needs manual review.")
        decoded = {}
        for name, record in value["files"].items():
            payload = base64.b64decode(record["new" if committed else "old"], validate=True)
            if content_digest(payload) != record["new_digest" if committed else "old_digest"]:
                reject("Upgrade journal digest mismatch.")
            decoded[name] = payload
        for name, record in value["files"].items():
            payload = decoded[name]
            # Payloads are validated together before the first replacement.
            temporary = (root / name).with_name(Path(name).name + ".upgrade-tmp")
            if temporary.exists():
                if temporary.is_symlink() or content_digest(temporary.read_bytes()) not in {
                    record["old_digest"],
                    record["new_digest"],
                }:
                    reject("Unowned upgrade staging file.")
                temporary.unlink()
            atomic(root / name, payload)
        journal.unlink()
        sql.commit()
    finally:
        sql.close()
    return {
        "recovered": True,
        "action": "finalized" if committed else "rolled_back",
        "to_version": TARGET if committed else value["from_version"],
    }


def upgrade(root, *, apply=False, backup=None):
    root = configuration_root(root)
    report = preflight(root)
    if report["pending_recovery"]:
        reject("Interrupted upgrade requires devroster upgrade --recover before proceeding.")
    if not apply or not report["changes"]:
        return dict(report, applied=False)
    originals = inputs(root)
    db = Database(root / "state.db")
    sql = db.connect()
    try:
        before = fingerprint(sql)
    finally:
        sql.close()
    destination = (
        Path(backup)
        if backup
        else root.parent.parent
        / (root.parent.name + "-agent-dev-backups")
        / (time.strftime("upgrade-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".zip")
    )
    bundle = create_bundle(root, destination)
    store = WorkflowRepository(db, secrets=registered_secrets(root))
    marker = json.loads(originals["workspace.json"])
    marker["engine_version"] = TARGET
    profile = json.loads(originals["config/profile.json"])
    profile["execution_limits"] = limits(profile.get("execution_limits"))
    modified = {
        "workspace.json": (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode(),
        "config/profile.json": (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode(),
    }
    upgrade_id = uuid.uuid4().hex
    journal = root / "upgrade-journal.json"
    try:
        with db.transaction() as sql:
            idle(sql)
            if fingerprint(sql) != before or inputs(root) != originals:
                reject("Workspace changed during backup; retry when idle.")
            value = {
                "format_version": 1,
                "id": upgrade_id,
                "from_version": report["from_version"],
                "files": {
                    name: {
                        "old": base64.b64encode(old).decode(),
                        "new": base64.b64encode(modified[name]).decode(),
                        "old_digest": content_digest(old),
                        "new_digest": content_digest(modified[name]),
                    }
                    for name, old in originals.items()
                },
            }
            with journal.open("x", encoding="utf-8") as stream:
                os.chmod(journal, 0o600)
                stream.write(json.dumps(value))
                stream.flush()
                os.fsync(stream.fileno())
            for row in sql.execute(
                "SELECT p.* FROM project_profile_versions p WHERE version=(SELECT MAX(version) FROM project_profile_versions WHERE project_id=p.project_id)"
            ).fetchall():
                updated = json.loads(row["content_json"])
                updated["execution_limits"] = limits(updated.get("execution_limits"))
                if digest(updated) != row["digest"]:
                    safe = json.loads(store._json(updated))
                    sql.execute(
                        "INSERT INTO project_profile_versions VALUES(?,?,?,?)",
                        (row["project_id"], row["version"] + 1, digest(safe), canonical(safe)),
                    )
                    sql.execute(
                        "UPDATE projects SET version=version+1 WHERE id=?", (row["project_id"],)
                    )
                    store._event(
                        sql,
                        row["project_id"],
                        "ProfileRevised",
                        {"profile_version": row["version"] + 1, "reason": "upgrade"},
                    )
            for name, payload in modified.items():
                atomic(root / name, payload)
            store._event(
                sql,
                marker["project_id"],
                "WorkspaceUpgraded",
                {
                    "upgrade_id": upgrade_id,
                    "from_version": report["from_version"],
                    "to_version": TARGET,
                },
            )
    except BaseException:
        # Keep the durable journal if rollback itself is interrupted/fails.
        if journal.exists():
            recover(root)
        raise
    journal.unlink()
    return dict(
        report,
        applied=True,
        backup=bundle,
        pending_recovery=False,
        operator_actions=[
            "Run devroster doctor. Existing tasks keep their approved snapshots; new plans use revised profiles."
        ],
    )
