"""Shared defaults of one management root, versioned in ``global/settings.db``.

The global store is separate from every project database: saving shared defaults never
writes a project, and a project only changes when it explicitly applies a revision
(``ProjectSetup.apply_global``). Reads never create the store.
"""

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from agent_dev.adapters.project_setup import private_directory
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.core.storage_locations import management_root
from agent_dev.domain.content import canonical, digest
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.settings_resolution import validate_global

# v2 adds global_ai_versions and global_ai_auth (additive; v1 stores upgrade on first write).
SCHEMA_VERSION = 2


class GlobalStore:
    """Connections to the management root's shared settings database."""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else management_root()
        self.directory = self.root / "global"
        self.path = self.directory / "settings.db"

    def _check(self):
        if self.directory.is_symlink() or self.path.is_symlink():
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "공통 설정 위치에 심볼릭 링크를 사용할 수 없습니다."
            )

    @staticmethod
    def _version(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "공통 설정 DB가 이 앱보다 새 버전입니다.")
        return version

    @contextlib.contextmanager
    def reader(self):
        """A read-only connection, or None while the store does not exist yet."""
        self._check()
        if not self.path.is_file():
            yield None
            return
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn if self._version(conn) else None
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self):
        self._check()
        private_directory(self.directory)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            Database._enable_wal(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                if self._version(conn) < SCHEMA_VERSION:
                    schema = Path(__file__).with_name("global_schema.sql").read_text(encoding="utf-8")
                    Database._run_statements(conn, schema)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()
        self.path.chmod(0o600)

    @staticmethod
    def event(conn, kind, revision, payload):
        conn.execute(
            "INSERT INTO global_events VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, kind, revision, canonical(payload), time.time()),
        )


class GlobalSettings(GlobalStore):
    def read(self, revision=None) -> dict:
        """``{"revision", "digest", "values"}``; revision 0 means no shared defaults yet."""
        with self.reader() as conn:
            if conn is None:
                rows = []
            elif revision is None:
                rows = conn.execute(
                    "SELECT * FROM global_settings_versions ORDER BY revision DESC LIMIT 1"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM global_settings_versions WHERE revision=?", (revision,)
                ).fetchall()
        if not rows:
            if revision not in {None, 0}:
                raise DomainError(ErrorCode.INVALID_INPUT, "선택한 공통 설정 revision이 없습니다.")
            return {"revision": 0, "digest": digest({}), "values": {}}
        row = rows[0]
        return {
            "revision": row["revision"],
            "digest": row["digest"],
            "values": json.loads(row["content_json"]),
        }

    def save(self, values: dict, expected_revision: int) -> dict:
        from agent_dev.adapters.ai_providers import PROVIDERS

        normalized = validate_global(values)
        provider = normalized.get("ai", {}).get("provider", "")
        if provider and provider not in PROVIDERS:
            raise DomainError(ErrorCode.INVALID_INPUT, "지원하는 기본 AI 도구를 선택해주세요.")
        if type(expected_revision) is not int or expected_revision < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "최신 공통 설정을 불러온 뒤 저장해주세요.")
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT COALESCE(MAX(revision),0) FROM global_settings_versions"
            ).fetchone()[0]
            if current != expected_revision:
                raise DomainError(
                    ErrorCode.STALE_VERSION,
                    "다른 화면에서 공통 설정이 바뀌었습니다. 다시 불러온 뒤 저장해주세요.",
                )
            revision, value_digest = current + 1, digest(normalized)
            conn.execute(
                "INSERT INTO global_settings_versions VALUES(?,?,?,?)",
                (revision, canonical(normalized), value_digest, time.time()),
            )
            self.event(
                conn,
                "GlobalSettingsSaved",
                revision,
                {"digest": value_digest, "groups": sorted(normalized)},
            )
        return {"revision": revision, "digest": value_digest, "values": normalized}
