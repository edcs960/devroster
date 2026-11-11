"""Short-lived connections, serialized writes and explicit migration transactions."""

import contextlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

# v4 adds configuration_provenance and shared_connection_bindings (additive only).
SCHEMA_VERSION = 4


class Database:
    def __init__(self, path: Path, *, read_only=False):
        original = Path(path).expanduser().absolute()
        if any(part.is_symlink() for part in (original, original.parent)):
            raise ValueError("Database paths cannot contain symlinks.")
        self.path = original.resolve()
        self.read_only = read_only
        if read_only and not self.path.is_file():
            raise FileNotFoundError(self.path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # A state DB this machine creates is trusted; one that arrived with a folder is not.
        created = not read_only and self.path.name == "state.db" and not self.path.exists()
        connection = self.connect()
        try:
            current_version = self._version(connection)
            self._supported(current_version)
            if read_only:
                connection.execute("PRAGMA query_only=ON")
                return
            self.path.chmod(0o600)
            self._enable_wal(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Re-read after acquiring ownership: another initializer may have upgraded.
                current_version = self._version(connection)
                self._supported(current_version)
                if 0 < current_version < SCHEMA_VERSION:
                    self._pre_upgrade_backup(connection)
                self._run_statements(connection, Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
                for version in range(1, SCHEMA_VERSION + 1):
                    connection.execute(
                        "INSERT OR IGNORE INTO schema_versions VALUES(?,?)", (version, time.time())
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Database foreign-key check failed.")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()
        if created:
            from agent_dev.adapters import workspace_trust
            from agent_dev.domain.errors import DomainError

            try:
                workspace_trust.record_created(self.path)
            except (DomainError, OSError, ValueError, TypeError):
                pass  # Without a record the workspace asks for trust; it never runs unchecked.

    @staticmethod
    def _enable_wal(connection, timeout=10.0):
        """Switch to WAL once; concurrent initializers may hold the lock briefly.

        Changing the journal mode needs an exclusive lock and SQLite can report
        "database is locked" without waiting on the busy handler, so retry with a
        bounded backoff. The whole call, including SQLite's own busy waits, stays
        within ``timeout``: each attempt's busy_timeout is capped to the remaining
        budget and the connection's busy_timeout is restored afterwards. The real
        error is re-raised when the budget is spent.
        """
        deadline, delay = time.monotonic() + timeout, 0.01
        original = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        try:
            while True:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                connection.execute(f"PRAGMA busy_timeout={remaining}")
                try:
                    if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                        connection.execute("PRAGMA journal_mode=WAL")
                    return
                except sqlite3.OperationalError as error:
                    locked = "locked" in str(error) or "busy" in str(error)
                    if not locked or time.monotonic() + delay > deadline:
                        raise
                time.sleep(delay)
                delay = min(delay * 2, 0.25)
        finally:
            connection.execute(f"PRAGMA busy_timeout={int(original)}")

    @staticmethod
    def _version(connection):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_versions'"
        ).fetchone()
        return (
            connection.execute("SELECT COALESCE(MAX(version),0) FROM schema_versions").fetchone()[0]
            if exists
            else 0
        )

    @staticmethod
    def _supported(version):
        if version > SCHEMA_VERSION:
            from agent_dev.domain.errors import DomainError, ErrorCode

            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Database schema is newer than this application."
            )

    def _pre_upgrade_backup(self, connection):
        """Hold writer ownership while snapshotting the committed pre-upgrade database."""
        import os
        import tempfile
        import uuid

        # v1/v2 databases keep their established ".pre-v3-backup" name.
        target_version = max(3, self._version(connection) + 1)
        base = Path(str(self.path) + f".pre-v{target_version}-backup")
        target = base if not base.exists() else Path(str(base) + "." + uuid.uuid4().hex)
        handle, temporary = tempfile.mkstemp(
            prefix=f".pre-v{target_version}-", dir=self.path.parent
        )
        os.close(handle)
        temporary = Path(temporary)
        try:
            with contextlib.closing(
                sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
            ) as source:
                backup = sqlite3.connect(temporary)
                try:
                    source.backup(backup)
                    backup.execute("PRAGMA journal_mode=DELETE")
                    if (
                        backup.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                        or backup.execute("PRAGMA foreign_key_check").fetchone()
                    ):
                        raise ValueError("Pre-upgrade backup integrity check failed.")
                    if self._version(backup) != self._version(connection):
                        raise ValueError("Pre-upgrade backup source version changed.")
                finally:
                    backup.close()
            # Exclusive publication: never overwrite a preexisting artifact, including symlinks.
            os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _run_statements(conn, sql: str) -> None:
        """Preserve leading comments and transaction ownership (executescript commits)."""
        buf = ""
        for line in sql.splitlines(keepends=True):
            buf += line
            if sqlite3.complete_statement(buf):
                conn.execute(buf)
                buf = ""
        if buf.strip():
            conn.execute(buf)

    def connect(self):
        location = self.path.as_uri() + "?mode=ro" if self.read_only else str(self.path)
        connection = sqlite3.connect(location, uri=self.read_only, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if self.read_only:
            connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def transaction(self):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def rows(self, sql: str, parameters: tuple = ()) -> list[dict]:
        connection = self.connect()
        try:
            return [dict(row) for row in connection.execute(sql, parameters)]
        finally:
            connection.close()

    def backup(self, destination: Path) -> None:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        source, backup = self.connect(), sqlite3.connect(target)
        try:
            source.backup(backup)
        finally:
            source.close()
            backup.close()
        target.chmod(0o600)
