"""Observable release gates, reversible exports and bounded retention."""

import json
import sqlite3
from pathlib import Path

from agent_dev.domain.content import content_digest
from agent_dev.domain.errors import DomainError, ErrorCode


class Operations:
    def __init__(self, repository):
        self.repository = repository

    def export(self, destination):
        destination = Path(destination)
        if destination.exists():
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Export destination already exists.")
        self.repository.db.backup(destination)
        manifest = {
            "schema_version": 1,
            "sha256": content_digest(destination.read_bytes()),
            "source": str(self.repository.db.path),
            "created_at": self.repository.clock(),
        }
        destination.with_suffix(destination.suffix + ".manifest.json").write_text(
            json.dumps(manifest, indent=2)
        )
        return manifest

    @staticmethod
    def restore(source, destination):
        source, destination = Path(source), Path(destination)
        manifest = json.loads(source.with_suffix(source.suffix + ".manifest.json").read_text())
        if content_digest(source.read_bytes()) != manifest["sha256"] or destination.exists():
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Backup digest mismatch or restore destination exists."
            )
        connection = sqlite3.connect(source)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid backup database.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(destination)
            try:
                connection.backup(target)
            finally:
                target.close()
        finally:
            connection.close()
        destination.chmod(0o600)
        return {"restored": str(destination)}

    def observe_release(self, release_id, comparison, rollback_report):
        if not release_id or not comparison.get("zero_diff") or not rollback_report.get("passed"):
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Zero-diff and successful rollback evidence required."
            )
        with self.repository.db.transaction() as sql:
            sql.execute(
                "INSERT INTO releases VALUES(?,?,1,1)", (release_id, self.repository.clock())
            )
            self.repository._event(
                sql,
                release_id,
                "ReleaseObserved",
                {"comparison": comparison, "rollback": rollback_report},
            )

    def retirement_ready(self):
        rows = self.repository.db.rows(
            "SELECT * FROM releases WHERE zero_diff=1 AND rollback_passed=1"
        )
        return len(rows) >= 2

    def projection(self, aggregate_id, after=0):
        return self.repository.db.rows(
            "SELECT * FROM events WHERE aggregate_id=? AND version>? ORDER BY version",
            (aggregate_id, after),
        )

    def retention(self, output_root, *, days=90, apply=False):
        if days < 30:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Minimum retention is 30 days.")
        root = Path(output_root).resolve()
        cutoff = self.repository.clock() - days * 86400
        rows = self.repository.db.rows(
            "SELECT a.* FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE a.created_at<? "
            "AND t.state IN ('completed','cancelled') AND NOT EXISTS "
            "(SELECT 1 FROM leases l WHERE l.resource='task:'||t.id AND l.expires_at>?)",
            (cutoff, self.repository.clock()),
        )
        candidates = []
        for row in rows:
            path = (root / row["path"]).resolve()
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or content_digest(path.read_bytes()) != row["digest"]
            ):
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Retention evidence scope or digest mismatch."
                )
            candidates.append(row)
        if apply:
            with self.repository.db.transaction() as sql:
                for row in candidates:
                    # Recheck ownership in the write transaction.
                    if sql.execute(
                        "SELECT 1 FROM leases WHERE resource=? AND expires_at>?",
                        ("task:" + row["task_id"], self.repository.clock()),
                    ).fetchone():
                        raise DomainError(
                            ErrorCode.LEASE_CONFLICT, "Task became active during retention."
                        )
                    sql.execute("DELETE FROM artifacts WHERE id=?", (row["id"],))
                    self.repository._event(
                        sql,
                        row["task_id"],
                        "EvidenceRetainedAsDigest",
                        {"path": row["path"], "digest": row["digest"]},
                    )
            # Commit provenance before deleting bytes. A crash leaves recoverable orphan
            # files, never database references to evidence that was already deleted.
            for row in candidates:
                (root / row["path"]).unlink(missing_ok=True)
        return {
            "dry_run": not apply,
            "artifacts": [r["path"] for r in candidates],
            "events_deleted": 0,
        }

    def rollback(self, configuration_root):
        from agent_dev.core.io import write_json
        from agent_dev.domain.status import TaskStatus
        from agent_dev.domain.transitions import transition_task

        configuration_root = Path(configuration_root)
        config_path = configuration_root / "config/runtime.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        config["orchestrator_v2"] = {
            "read": False,
            "dual_write": False,
            "execute": False,
            "publish": False,
            "legacy_readonly": False,
        }
        write_json(config_path, config)
        with self.repository.db.transaction() as sql:
            # Supervisors observe invalidated fencing tokens and stop their provider groups.
            sql.execute(
                "UPDATE leases SET token=token+1,expires_at=0 WHERE expires_at>?",
                (self.repository.clock(),),
            )
            sql.execute(
                "UPDATE phase_attempts SET state='interrupted',ended_at=? WHERE state='running'",
                (self.repository.clock(),),
            )
            sql.execute("UPDATE commands SET state='interrupted' WHERE state='running'")
            tasks = sql.execute(
                "SELECT id,state FROM tasks WHERE state IN ('running','verifying','waiting_for_capacity')"
            ).fetchall()
            for task in tasks:
                target = transition_task(TaskStatus(task["state"]), TaskStatus.BLOCKED)
                sql.execute(
                    "UPDATE tasks SET state=?,version=version+1 WHERE id=?",
                    (target.value, task["id"]),
                )
                self.repository._event(
                    sql, task["id"], "ExecutionRolledBack", {"previous_state": task["state"]}
                )
        return {
            "flags": config["orchestrator_v2"],
            "executions_interrupted": len(tasks),
            "database_preserved": True,
        }
