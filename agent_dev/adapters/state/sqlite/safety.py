"""Durable leases, command deduplication and bounded notification delivery."""

import json
import threading
from contextlib import contextmanager

from agent_dev.adapters.state.sqlite.store import WorkflowRepository, identifier
from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError, ErrorCode


class ExecutionSafety:
    def __init__(self, repository: WorkflowRepository):
        self.repository = repository
        self.db, self.clock = repository.db, repository.clock

    def acquire(self, resource: str, holder: str, ttl: float = 300) -> dict:
        if not resource or not holder or ttl <= 0:
            raise DomainError(
                ErrorCode.INVALID_INPUT, "Lease resource, holder and positive TTL required."
            )
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE resource=?", (resource,)
            ).fetchone()
            if row and row["expires_at"] > self.clock():
                raise DomainError(ErrorCode.LEASE_CONFLICT, "Resource already leased.")
            token = row["token"] + 1 if row else 1
            now = self.clock()
            connection.execute(
                "INSERT INTO leases VALUES(?,?,?,?,?) ON CONFLICT(resource) DO UPDATE "
                "SET holder=excluded.holder,token=excluded.token,expires_at=excluded.expires_at,"
                "heartbeat_at=excluded.heartbeat_at",
                (resource, holder, token, now + ttl, now),
            )
            return {"resource": resource, "holder": holder, "token": token}

    def heartbeat(self, lease: dict, ttl: float = 300) -> None:
        with self.db.transaction() as connection:
            self.repository.assert_lease(connection, lease)
            connection.execute(
                "UPDATE leases SET expires_at=?,heartbeat_at=? WHERE resource=?",
                (self.clock() + ttl, self.clock(), lease["resource"]),
            )

    def release(self, lease: dict) -> None:
        # Preserve fencing counter even after release.
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE leases SET expires_at=0 WHERE resource=? AND holder=? AND token=?",
                (lease["resource"], lease["holder"], lease["token"]),
            )

    @contextmanager
    def lease(self, resource: str, ttl: float = 300, heartbeat_seconds: float = 30):
        grant = self.acquire(resource, identifier(), ttl)
        stop = threading.Event()
        errors = []

        def keep_alive():
            while not stop.wait(min(heartbeat_seconds, ttl / 3)):
                try:
                    self.heartbeat(grant, ttl)
                except Exception as exc:  # noqa: BLE001 - surface heartbeat failure to owner
                    errors.append(exc)
                    return

        worker = threading.Thread(target=keep_alive, daemon=True)
        worker.start()
        try:
            yield grant
            if errors:
                raise DomainError(
                    ErrorCode.LEASE_CONFLICT, "Heartbeat lost ownership."
                ) from errors[0]
        finally:
            stop.set()
            worker.join(timeout=5)
            self.release(grant)

    def command(self, key: str, request: dict, operation):
        request_digest = digest(request)
        with self.lease(f"command:{key}") as grant:
            with self.db.transaction() as connection:
                self.repository.assert_lease(connection, grant)
                row = connection.execute("SELECT * FROM commands WHERE key=?", (key,)).fetchone()
                if row and row["request_digest"] != request_digest:
                    raise DomainError(
                        ErrorCode.POLICY_VIOLATION, "Idempotency key reused with another request."
                    )
                if row and row["state"] == "completed":
                    return json.loads(row["result_json"])
                connection.execute(
                    "INSERT INTO commands VALUES(?,?,'running',NULL,?) "
                    "ON CONFLICT(key) DO UPDATE SET state='running'",
                    (key, request_digest, self.clock()),
                )
            # External side effects need their own operation key. A killed
            # command may be retried after its lease expires.
            try:
                result = operation()
            except BaseException:
                with self.db.transaction() as connection:
                    self.repository.assert_lease(connection, grant)
                    connection.execute(
                        "UPDATE commands SET state='interrupted' WHERE key=?", (key,)
                    )
                raise
            with self.db.transaction() as connection:
                self.repository.assert_lease(connection, grant)
                connection.execute(
                    "UPDATE commands SET state='completed',result_json=? WHERE key=?",
                    (self.repository._json(result), key),
                )
            return result

    def deliver(
        self,
        sender,
        *,
        channel: str = "workflow",
        limit: int = 100,
        max_attempts: int = 3,
        project_id: str = "",
    ) -> dict:
        """At-least-once; sender must honor the supplied durable delivery key."""
        delivered, failed = 0, 0
        with self.lease(f"outbox:{channel}") as grant:
            rows = self.db.rows(
                "SELECT * FROM notification_outbox WHERE channel=? AND state='pending' "
                "AND next_attempt_at<=? AND attempts<? AND (?='' OR json_extract(payload_json,'$.project_id')=?) "
                "ORDER BY next_attempt_at LIMIT ?",
                (channel, self.clock(), max_attempts, project_id, project_id, limit),
            )
            for row in rows:
                try:
                    sender(row["id"], json.loads(row["payload_json"]))
                except Exception:  # noqa: BLE001 - delivery failures are durable, task execution is independent
                    failed += 1
                    with self.db.transaction() as connection:
                        self.repository.assert_lease(connection, grant)
                        attempts = row["attempts"] + 1
                        connection.execute(
                            "UPDATE notification_outbox SET attempts=?,state=?,next_attempt_at=? WHERE id=?",
                            (
                                attempts,
                                "dead" if attempts >= max_attempts else "pending",
                                self.clock() + min(3600, 2**attempts),
                                row["id"],
                            ),
                        )
                else:
                    delivered += 1
                    with self.db.transaction() as connection:
                        self.repository.assert_lease(connection, grant)
                        connection.execute(
                            "UPDATE notification_outbox SET state='delivered' WHERE id=?",
                            (row["id"],),
                        )
        return {"delivered": delivered, "failed": failed}

    def cooldown(self, connection_id: str, seconds: int) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO capacity VALUES(?,?) ON CONFLICT(connection_id) DO UPDATE SET ready_at=excluded.ready_at",
                (connection_id, self.clock() + seconds),
            )

    def available(self, connection_id: str) -> bool:
        rows = self.db.rows("SELECT ready_at FROM capacity WHERE connection_id=?", (connection_id,))
        return not rows or rows[0]["ready_at"] <= self.clock()
