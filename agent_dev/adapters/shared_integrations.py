"""Shared MCP connection definitions and explicit, version-pinned workspace bindings.

A shared definition (endpoint/command/transport/operations/allowed scopes and a credential
reference name) is versioned in the management root's ``global/settings.db``. A project
uses one only after ``bind``: the chosen version, operations and scopes (a subset of the
shared ones) are materialized as an ordinary ``tool_connections`` row plus an immutable
``shared_connection_bindings`` record, in one project transaction. New shared versions
never rewrite a binding; disabling a shared connection blocks every binding at once.
"""

import json
import re
from typing import NoReturn

from agent_dev.adapters.environment import validate_key
from agent_dev.adapters.global_settings import GlobalStore
from agent_dev.domain.content import canonical, digest
from agent_dev.domain.errors import DomainError, ErrorCode

IDENTIFIER = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,79}")
DEFINITION_KEYS = (
    "provider",
    "name",
    "transport",
    "endpoint",
    "command",
    "scopes",
    "credential_ref",
    "local",
    "operations",
    "bound_arguments",
    "environment_keys",
    "token_mode",
    "oauth_client_id",
    "supported_client",
)


def invalid(message, code=ErrorCode.INVALID_INPUT) -> NoReturn:
    raise DomainError(code, message)


def _identifier(value, label):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        invalid(label + "는 영문으로 시작하는 이름이어야 합니다.")
    return value


def _definition(conn, connection_id, version):
    row = conn.execute(
        "SELECT * FROM global_connection_versions WHERE connection_id=? AND version=?",
        (connection_id, version),
    ).fetchone()
    return None if row is None else {"digest": row["digest"], **json.loads(row["content_json"])}


def binding_state(shared):
    """None when a binding may be used now; otherwise a stable blocking reason."""
    with GlobalStore().reader() as conn:
        if conn is None:
            return "SHARED_CONNECTION_MISSING"
        definition = _definition(conn, shared.get("connection_id"), shared.get("version"))
        state = conn.execute(
            "SELECT enabled FROM global_connection_state WHERE connection_id=?",
            (shared.get("connection_id"),),
        ).fetchone()
    if definition is None or state is None or definition["digest"] != shared.get("digest"):
        return "SHARED_CONNECTION_MISSING"
    return None if state["enabled"] == 1 else "SHARED_CONNECTION_DISABLED"


class SharedIntegrations:
    def __init__(self, root=None, *, integrations=None):
        # Registration, listing and disabling are management-level; only bind needs a project.
        self.root = root
        self._integrations = integrations
        self.global_store = GlobalStore()

    @property
    def integrations(self):
        from agent_dev.adapters.integrations import Integrations

        if self._integrations is None:
            if self.root is None:
                invalid("공통 연결을 지정할 프로젝트를 선택해주세요.")
            self._integrations = Integrations(self.root)
        return self._integrations

    @property
    def store(self):
        return self.integrations.store

    def register(self, connection_id, config, expected_version=0) -> dict:
        """Add a new immutable version of a shared definition (no secret values)."""
        from agent_dev.adapters.integrations import Integrations

        _identifier(connection_id, "공통 연결 ID")
        if not isinstance(config, dict) or "id" in config:
            invalid("공통 연결 정의 형식을 확인해주세요.")
        row = Integrations.connection_row(dict(config, id=connection_id), "")
        definition = {key: row[key] for key in DEFINITION_KEYS if key in row}
        definition_digest = digest(definition)
        if type(expected_version) is not int or expected_version < 0:
            invalid("최신 공통 연결을 불러온 뒤 저장해주세요.")
        with self.global_store.transaction() as conn:
            current = conn.execute(
                "SELECT COALESCE(MAX(version),0) FROM global_connection_versions "
                "WHERE connection_id=?",
                (connection_id,),
            ).fetchone()[0]
            if current != expected_version:
                raise DomainError(
                    ErrorCode.STALE_VERSION, "공통 연결이 바뀌었습니다. 다시 불러와주세요."
                )
            version = current + 1
            conn.execute(
                "INSERT INTO global_connection_versions VALUES(?,?,?,?,?)",
                (
                    connection_id,
                    version,
                    canonical(definition),
                    definition_digest,
                    __import__("time").time(),
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO global_connection_state VALUES(?,1,1)", (connection_id,)
            )
            self.global_store.event(
                conn,
                "SharedConnectionRegistered",
                version,
                {"connection_id": connection_id, "version": version, "digest": definition_digest},
            )
        return self._summary(connection_id)

    def _summary(self, connection_id):
        return next(row for row in self.definitions() if row["connection_id"] == connection_id)

    def definitions(self) -> list[dict]:
        with self.global_store.reader() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT v.connection_id,v.version,v.digest,v.content_json,s.enabled,s.state_revision "
                "FROM global_connection_versions v JOIN global_connection_state s "
                "USING(connection_id) WHERE v.version=(SELECT MAX(version) FROM "
                "global_connection_versions WHERE connection_id=v.connection_id) "
                "ORDER BY v.connection_id"
            ).fetchall()
        return [
            {
                "connection_id": row["connection_id"],
                "version": row["version"],
                "digest": row["digest"],
                "definition": json.loads(row["content_json"]),
                "enabled": row["enabled"] == 1,
                "state_revision": row["state_revision"],
            }
            for row in rows
        ]

    def disable(self, global_id: str, expected_revision: int) -> dict:
        """Block current use by every binding; definitions stay as tombstones."""
        with self.global_store.transaction() as conn:
            state = conn.execute(
                "SELECT * FROM global_connection_state WHERE connection_id=?", (global_id,)
            ).fetchone()
            if state is None:
                invalid("등록된 공통 연결을 선택해주세요.")
            if type(expected_revision) is not int or state["state_revision"] != expected_revision:
                raise DomainError(
                    ErrorCode.STALE_VERSION, "공통 연결 상태가 바뀌었습니다. 다시 불러와주세요."
                )
            revision = expected_revision + 1
            conn.execute(
                "UPDATE global_connection_state SET enabled=0,state_revision=? "
                "WHERE connection_id=? AND state_revision=?",
                (revision, global_id, expected_revision),
            )
            self.global_store.event(
                conn, "SharedConnectionDisabled", revision, {"connection_id": global_id}
            )
        return self._summary(global_id)

    def bind(
        self,
        workspace_id: str,
        local_id: str,
        global_id: str,
        version: int,
        scopes: list[str],
        operations: list[str],
        expected_version: int,
        *,
        credential_ref: str = "",
    ) -> dict:
        """Materialize one pinned shared version as this workspace's connection ``local_id``.

        ``credential_ref`` selects a key of this project's private environment; empty uses
        the shared connection's own credential. Scopes and operations can only narrow.
        """
        from agent_dev.adapters.integrations import Integrations

        locations = self.store.locations
        if workspace_id != locations.workspace_id:
            invalid(
                "현재 workspace에만 공통 연결을 지정할 수 있습니다.", ErrorCode.POLICY_VIOLATION
            )
        _identifier(local_id, "연결 ID")
        _identifier(global_id, "공통 연결 ID")
        with self.global_store.reader() as conn:
            definition = None if conn is None else _definition(conn, global_id, version)
            state = (
                None
                if conn is None
                else conn.execute(
                    "SELECT enabled FROM global_connection_state WHERE connection_id=?",
                    (global_id,),
                ).fetchone()
            )
        if definition is None or state is None:
            invalid("선택한 공통 연결 버전이 없습니다.")
        if state["enabled"] != 1:
            invalid("비활성화된 공통 연결은 지정할 수 없습니다.", ErrorCode.POLICY_VIOLATION)
        for label, values, allowed in (
            ("자료 범위", scopes, definition["scopes"]),
            ("도구", operations, list(definition["operations"])),
        ):
            if (
                not isinstance(values, list)
                or not values
                or len(set(values)) != len(values)
                or any(value not in allowed for value in values)
            ):
                invalid(
                    f"공통 연결이 허용한 {label} 안에서만 선택할 수 있습니다.",
                    ErrorCode.POLICY_VIOLATION,
                )
        if credential_ref:
            validate_key(credential_ref)
            credential = "project:" + credential_ref
        elif definition.get("credential_ref"):
            credential = f"shared:{global_id}:{definition['credential_ref']}"
        else:
            credential = ""
        config = {
            key: definition[key]
            for key in DEFINITION_KEYS
            if key in definition and key not in {"credential_ref", "scopes", "operations"}
        }
        config.update(
            id=local_id,
            scopes=list(scopes),
            operations={name: definition["operations"][name] for name in operations},
        )
        row = Integrations.connection_row(config, self.store.project)
        binding = {
            "connection_id": global_id,
            "version": version,
            "digest": definition["digest"],
            "credential": credential,
        }
        row["shared"] = binding
        return self.store.save(
            row,
            expected_version,
            binding=dict(binding, scopes=list(scopes), operations=sorted(operations)),
        )
