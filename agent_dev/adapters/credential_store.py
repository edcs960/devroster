"""Resolve credential references to private values inside one workspace, at execution only.

References never contain values:

- ``project:<KEY>``: the workspace's own private environment file.
- ``shared:<connection id>:<KEY>``: a shared connection's credential under
  ``<management root>/credentials/shared/``; optionally bound to one resource.

Resolved values are handed to the caller that performs the request and must never be
returned by an API, written to a snapshot, export, event or log.
"""

import json
import re
from typing import NoReturn

from agent_dev.adapters.environment import read_environment, validate_key
from agent_dev.adapters.project_setup import atomic_json, private_directory
from agent_dev.core.storage_locations import WorkspaceLocations, management_root
from agent_dev.domain.errors import DomainError, ErrorCode

IDENTIFIER = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,79}")
LIMIT = 500_000


def denied(message, code=ErrorCode.AUTH_REQUIRED) -> NoReturn:
    raise DomainError(code, message)


def shared_directory():
    return management_root() / "credentials" / "shared"


def _shared_path(connection_id):
    if not isinstance(connection_id, str) or not IDENTIFIER.fullmatch(connection_id):
        denied("공통 연결 ID를 확인해주세요.", ErrorCode.INVALID_INPUT)
    directory = shared_directory()
    if directory.is_symlink() or directory.parent.is_symlink():
        denied("인증 저장 위치에 심볼릭 링크를 사용할 수 없습니다.", ErrorCode.POLICY_VIOLATION)
    return directory / (connection_id + ".json")


def _read_shared(connection_id):
    path = _shared_path(connection_id)
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > LIMIT:
        denied("공통 인증 파일을 확인해주세요.", ErrorCode.POLICY_VIOLATION)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("values"), dict):
        denied("공통 인증 파일 형식을 확인해주세요.", ErrorCode.POLICY_VIOLATION)
    return value


class CredentialStore:
    def __init__(self, locations: WorkspaceLocations):
        self.locations = locations

    @staticmethod
    def save_shared(connection_id, key, value, *, resource=""):
        """Store one private value of a shared connection (same device only)."""
        validate_key(key)
        if not isinstance(value, str) or not value or len(value) > 8000 or "\x00" in value:
            denied("인증 값 형식을 확인해주세요.", ErrorCode.INVALID_INPUT)
        if not isinstance(resource, str) or len(resource) > 2000:
            denied("인증 대상 주소를 확인해주세요.", ErrorCode.INVALID_INPUT)
        path = _shared_path(connection_id)
        current = _read_shared(connection_id) or {"values": {}}
        if current.get("resource", "") not in {"", resource}:
            denied("다른 주소에 묶인 공통 인증입니다.", ErrorCode.POLICY_VIOLATION)
        private_directory(path.parent)
        atomic_json(path, {"resource": resource, "values": dict(current["values"], **{key: value})})

    @staticmethod
    def revoke_shared(connection_id):
        """Remove a shared connection's private values; every binding then needs re-auth."""
        _shared_path(connection_id).unlink(missing_ok=True)

    @staticmethod
    def shared_entry(connection_id) -> dict:
        """``{KEY: value}`` of one shared connection, for process environments only."""
        return dict((_read_shared(connection_id) or {}).get("values", {}))

    @staticmethod
    def remove_shared_key(connection_id, key):
        """Delete one private value of a shared connection; the others stay."""
        validate_key(key)
        path = _shared_path(connection_id)
        current = _read_shared(connection_id)
        if current is None or key not in current["values"]:
            return
        values = {k: v for k, v in current["values"].items() if k != key}
        atomic_json(path, dict(current, values=values))

    @staticmethod
    def shared_values():
        """All shared private values on this device, for redaction only."""
        directory = shared_directory()
        if not directory.is_dir() or directory.is_symlink():
            return ()
        values = []
        for path in sorted(directory.glob("*.json")):
            row = _read_shared(path.stem) if IDENTIFIER.fullmatch(path.stem) else None
            values.extend(v for v in (row or {}).get("values", {}).values() if v)
        return tuple(values)

    def resolve(self, reference: str, workspace_id: str, resource: str) -> dict[str, str]:
        """Private values for one reference; only inside this workspace's execution."""
        if workspace_id != self.locations.workspace_id:
            denied("다른 workspace의 인증을 사용할 수 없습니다.", ErrorCode.POLICY_VIOLATION)
        parts = reference.split(":") if isinstance(reference, str) else []
        if len(parts) == 2 and parts[0] == "project":
            validate_key(parts[1])
            values = read_environment(self.locations.config_root)
            if not values.get(parts[1]):
                denied("프로젝트 환경변수에 인증 값을 등록해주세요: " + parts[1])
            return {parts[1]: values[parts[1]]}
        if len(parts) == 3 and parts[0] == "shared":
            validate_key(parts[2])
            row = _read_shared(parts[1])
            if row is None or not row["values"].get(parts[2]):
                denied("공통 연결 인증이 없거나 해지되었습니다. 다시 인증해주세요.")
            if row.get("resource") and row["resource"] != resource:
                denied("공통 인증이 묶인 주소와 연결 주소가 다릅니다.", ErrorCode.POLICY_VIOLATION)
            return {parts[2]: row["values"][parts[2]]}
        denied("인증 참조 형식을 확인해주세요.", ErrorCode.INVALID_INPUT)
