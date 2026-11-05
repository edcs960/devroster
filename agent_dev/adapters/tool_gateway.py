"""Registered tool grants and abortable operations; no ambient tool permissions."""

import json
from dataclasses import asdict
from urllib.parse import urlparse

from agent_dev.adapters.state.sqlite.store import identifier
from agent_dev.domain.content import redact
from agent_dev.domain.contracts import ToolConnection
from agent_dev.domain.errors import DomainError, ErrorCode

PRIVILEGED = {"design.write", "security.active_scan", "github.write"}


class ToolGateway:
    def __init__(self, repository):
        self.repository = repository

    def register(self, connection: ToolConnection) -> None:
        value = asdict(connection)
        value["capabilities"] = sorted(connection.capabilities)
        with self.repository.db.transaction() as sql:
            sql.execute(
                "INSERT INTO tool_connections VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET content_json=excluded.content_json",
                (connection.connection_id, connection.project_id, self.repository._json(value)),
            )
            self.repository._event(
                sql,
                connection.project_id,
                "ToolRegistered",
                {"connection_id": connection.connection_id},
            )

    def connection(self, connection_id):
        rows = self.repository.db.rows(
            "SELECT content_json FROM tool_connections WHERE id=?", (connection_id,)
        )
        if not rows:
            raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE, "Unregistered tool.")
        value = json.loads(rows[0]["content_json"])
        value["capabilities"] = frozenset(value["capabilities"])
        value["allowlist"] = tuple(value["allowlist"])
        return ToolConnection(**value)

    def grant(self, connection_id, capability, scope, actor, *, task_id, phase_id):
        connection = self.connection(connection_id)
        task = self.repository.task(task_id)
        phases = self.repository.snapshot(task_id)["resolved"].get("phases", [])
        if (
            task.project_id != connection.project_id
            or not self.repository.approved(task_id)
            or phase_id not in {phase["id"] for phase in phases}
        ):
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED, "Tool grant requires its approved task and phase."
            )
        if (
            not actor
            or capability not in PRIVILEGED
            or capability not in connection.capabilities
            or scope not in connection.allowlist
        ):
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "Privileged capability, actor and registered scope required.",
            )
        grant_id = identifier()
        with self.repository.db.transaction() as sql:
            sql.execute(
                "INSERT INTO capability_grants VALUES(?,?,?,?,?,?,?,?,?,'approved')",
                (
                    grant_id,
                    connection_id,
                    task.project_id,
                    task_id,
                    phase_id,
                    capability,
                    scope,
                    actor,
                    self.repository.clock() + 300,
                ),
            )
            self.repository._event(
                sql,
                task_id,
                "ToolGrantIssued",
                {
                    "approval_id": grant_id,
                    "capability": capability,
                    "scope": scope,
                    "phase_id": phase_id,
                },
            )
        return grant_id

    def call(
        self,
        connection: ToolConnection,
        capability: str,
        scope: str,
        operation,
        *,
        approval_id: str = "",
        timeout_seconds: int = 30,
        task_id: str = "",
        phase_id: str = "",
    ):
        registered = self.connection(connection.connection_id)
        if registered != connection:
            raise DomainError(ErrorCode.STALE_VERSION, "Tool connection changed.")
        if (
            not connection.enabled
            or not connection.healthy
            or capability not in connection.capabilities
        ):
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE, "Tool capability disabled or unhealthy."
            )
        if not connection.authenticated:
            raise DomainError(ErrorCode.AUTH_REQUIRED, "Tool authentication required.")
        if scope not in connection.allowlist or not 1 <= timeout_seconds <= 1800:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Tool scope or timeout is not allowed.")
        if capability.startswith(("browser.", "security.active")) and urlparse(
            scope
        ).scheme not in {"http", "https"}:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Invalid test URL.")
        if not hasattr(operation, "execute"):
            raise DomainError(
                ErrorCode.INVALID_INPUT, "Tool operation must enforce timeout and cancellation."
            )
        if capability in PRIVILEGED:
            with self.repository.db.transaction() as sql:
                row = sql.execute(
                    "SELECT * FROM capability_grants WHERE id=? AND state='approved'",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    raise DomainError(
                        ErrorCode.APPROVAL_REQUIRED, "One-time privileged tool approval required."
                    )
                if (
                    row["capability"] != capability
                    or row["connection_id"] != connection.connection_id
                    or row["scope"] != scope
                    or row["task_id"] != task_id
                    or row["phase_id"] != phase_id
                    or row["expires_at"] <= self.repository.clock()
                    or not self.repository._approved(sql, task_id)
                ):
                    raise DomainError(
                        ErrorCode.POLICY_VIOLATION,
                        "Tool approval task, phase or scope differs; grant may have expired.",
                    )
                sql.execute(
                    "UPDATE capability_grants SET state='consumed' WHERE id=?", (approval_id,)
                )
        outcome = "failed"
        try:
            value = operation.execute(timeout_seconds)
            outcome = "passed"
            return redact(value, self.repository.secrets)
        finally:
            self.repository.event(
                connection.project_id,
                "ToolCall",
                {
                    "connection_id": connection.connection_id,
                    "capability": capability,
                    "scope": scope,
                    "outcome": outcome,
                },
            )
