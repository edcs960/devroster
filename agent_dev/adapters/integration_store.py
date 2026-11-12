"""Connection configuration shares existing SQLite ownership and event transactions."""

import json
from pathlib import Path

from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.workspace import configuration_root
from agent_dev.domain.content import canonical
from agent_dev.domain.errors import DomainError, ErrorCode


class IntegrationStore:
    def __init__(self, root):
        from agent_dev.adapters.project_setup import ProjectSetup
        from agent_dev.adapters.workflow_runtime import project_root
        from agent_dev.adapters.workspace_locator import workspace_locations

        self.config = configuration_root(Path(root))
        self.root = project_root(self.config)
        self.locations = workspace_locations(self.config)
        self.project = ProjectSetup(root).current_profile()[0]
        self.path = self.locations.state_db

    def database(self):
        db = Database(self.path)
        with db.transaction() as sql:
            sql.execute(
                "INSERT OR IGNORE INTO projects VALUES(?,?,?,'draft',0,?)",
                (self.project, self.root.name, self.root.name, __import__("time").time()),
            )
        return db

    def list(self):
        if not self.path.is_file():
            return []
        rows = Database(self.path, read_only=True).rows(
            "SELECT content_json FROM tool_connections WHERE project_id=?", (self.project,)
        )
        return [
            json.loads(r["content_json"])
            for r in rows
            if json.loads(r["content_json"]).get("integration") is True
        ]

    def get(self, key):
        row = next((r for r in self.list() if r["id"] == key), None)
        if row is None:
            raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE, "등록된 MCP 연결을 선택해주세요.")
        return row

    def save(self, row, expected_version=0, *, binding=None):
        db = self.database()
        storage_id = self.project + ":" + row["id"]
        with db.transaction() as sql:
            current = sql.execute(
                "SELECT project_id,content_json FROM tool_connections WHERE id=?", (storage_id,)
            ).fetchone()
            version = json.loads(current["content_json"]).get("version", 0) if current else 0
            if current and current["project_id"] != self.project:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "다른 프로젝트 연결을 변경할 수 없습니다."
                )
            previous = json.loads(current["content_json"]).get("shared") if current else None
            if binding is None and previous != row.get("shared"):
                # A shared binding changes only through an explicit, verified rebind.
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION,
                    "공통 연결을 사용하는 연결은 공통 연결 선택에서 다시 지정해주세요.",
                )
            if version != expected_version:
                raise DomainError(
                    ErrorCode.STALE_VERSION, "연결 설정이 변경되었습니다. 다시 불러와주세요."
                )
            row = dict(row, version=version + 1, integration=True)
            sql.execute(
                "INSERT INTO tool_connections VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET content_json=excluded.content_json",
                (storage_id, self.project, canonical(row)),
            )
            if binding is not None:
                sql.execute(
                    "INSERT INTO shared_connection_bindings VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        self.project,
                        storage_id,
                        row["version"],
                        binding["connection_id"],
                        binding["version"],
                        binding["digest"],
                        canonical(binding["scopes"]),
                        canonical(binding["operations"]),
                        canonical({"reference": binding["credential"]}),
                        __import__("time").time(),
                    ),
                )
            WorkflowRepository(db)._event(
                sql,
                self.project,
                "IntegrationConfigured",
                {"id": row["id"], "version": row["version"], "state": row["state"]},
            )
        return row

    def event(self, kind, payload):
        WorkflowRepository(self.database()).event(self.project, kind, payload)

    def import_rows(self, rows):
        db = self.database()
        result = []
        with db.transaction() as sql:
            for source in rows:
                row = dict(source, version=1, integration=True)
                try:
                    sql.execute("INSERT INTO tool_connections VALUES(?,?,?)",
                                (self.project + ":" + row["id"], self.project, canonical(row)))
                except __import__("sqlite3").IntegrityError as exc:
                    raise DomainError(ErrorCode.STALE_VERSION, "연결 목록이 변경되었습니다. 다시 가져와주세요.") from exc
                WorkflowRepository(db)._event(sql, self.project, "IntegrationConfigured",
                    {"id": row["id"], "version": 1, "state": row["state"]})
                result.append(row)
        return result
