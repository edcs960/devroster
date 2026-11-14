"""Resolve credential references to private values inside one workspace, at execution only.

References never contain values:

- ``project:<KEY>``: the workspace's own private environment file.
- ``shared:<connection id>:<KEY>``: a shared connection's credential under
  ``<management root>/credentials/shared/``; optionally bound to one resource.

A shared key can hold an environment variable name instead of its value (``env:NAME``
on the screens). Then nothing secret is written to disk: the value is read from the
process environment each time it is needed, so a shell profile, ``launchctl setenv`` or a
secret manager (``op run``, ``security find-generic-password`` in a profile) supplies it.

A shared value itself goes to the operating system's credential store when there is one
(``os_vault``: macOS keychain, Windows DPAPI); the file then keeps where it is (``vault``)
instead of the value. When the store cannot take it, the value stays in the file as before.

Resolved values are handed to the caller that performs the request and must never be
returned by an API, written to a snapshot, export, event or log.
"""

import json
import os
import re
import stat
from pathlib import Path
from typing import NoReturn

from agent_dev.adapters import os_vault
from agent_dev.adapters.environment import read_environment, validate_key
from agent_dev.adapters.project_setup import atomic_json, private_directory
from agent_dev.core.storage_locations import WorkspaceLocations, management_root
from agent_dev.domain.errors import DomainError, ErrorCode

IDENTIFIER = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,79}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
ENV_PREFIX = "env:"
LIMIT = 500_000


UNREADABLE = (
    "OS 보안 저장소(macOS 키체인, Windows 계정 보호)에 보관한 공통 연결 인증을 읽지 못했습니다. "
    "키체인 잠금을 풀고 다시 시도하거나, 다른 컴퓨터·계정에서 옮겨온 설정이면 인증 값을 다시 입력해주세요."
)


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
    env = value.setdefault("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(name, str) and ENV_NAME.fullmatch(name) for name in env.values()
    ):
        denied("공통 인증 파일 형식을 확인해주세요.", ErrorCode.POLICY_VIOLATION)
    kept = value.setdefault("vault", {})
    if not isinstance(kept, dict) or not all(
        isinstance(entry, dict) and isinstance(entry.get("kind"), str) and isinstance(entry.get("token"), str)
        for entry in kept.values()
    ):
        denied("공통 인증 파일 형식을 확인해주세요.", ErrorCode.POLICY_VIOLATION)
    return value


def _protect(connection_id, key, value):
    """Where the OS store keeps ``value`` now, or None when it stays in the file."""
    store = os_vault.vault()
    if store is None:
        return None
    try:
        return {"kind": store.kind, "token": store.protect(f"DevRoster/{connection_id}/{key}", value)}
    except os_vault.VaultError:
        return None


def _reveal(entry):
    store = os_vault.reader(entry["kind"])
    if store is None:
        raise os_vault.VaultError("unavailable")
    return store.reveal(entry["token"])


def _forget(entry):
    store = os_vault.reader(entry["kind"]) if entry else None
    if store is not None:
        store.forget(entry["token"])


def env_reference(value):
    """The variable name of an ``env:NAME`` input, or None for a literal value."""
    if not isinstance(value, str) or not value.startswith(ENV_PREFIX):
        return None
    name = value[len(ENV_PREFIX):].strip()
    if not ENV_NAME.fullmatch(name):
        denied("env: 뒤에 환경변수 이름(영문·숫자·_)을 적어주세요.", ErrorCode.INVALID_INPUT)
    return name


def missing_hint(name, platform=None, frozen=None):
    """How to give DevRoster's process the variable, for the way it was most likely started."""
    import sys

    platform = platform or sys.platform
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if platform == "darwin" and frozen:
        # An app opened from Finder or the Dock never sees the shell profile.
        return (f"Finder로 연 앱은 셸 환경변수를 받지 않습니다. 터미널에서 `launchctl setenv {name} <값>`을 "
                "실행한 뒤 앱을 다시 열거나, 터미널에서 `devroster app`으로 실행해주세요.")
    if platform.startswith("win"):
        return f"`setx {name} <값>`으로 사용자 환경변수를 등록한 뒤 앱을 다시 실행해주세요."
    return f"`export {name}=<값>`을 셸 설정에 넣고, 그 셸에서 `devroster app`을 다시 실행해주세요."


def _resolved(row):
    """Stored values that can be read, plus referenced environment values set right now."""
    values = dict(row.get("values", {}))
    for key, entry in row.get("vault", {}).items():
        try:
            values[key] = _reveal(entry)
        except os_vault.VaultError:
            pass  # Locked or moved to another computer: the same as not set, until it reads again.
    for key, name in row.get("env", {}).items():
        if os.environ.get(name):
            values[key] = os.environ[name]
    return values


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
        current = _read_shared(connection_id) or {"values": {}, "env": {}, "vault": {}}
        if current.get("resource", "") not in {"", resource}:
            denied("다른 주소에 묶인 공통 인증입니다.", ErrorCode.POLICY_VIOLATION)
        name = env_reference(value)
        values = {k: v for k, v in current["values"].items() if k != key}
        env = {k: v for k, v in current.get("env", {}).items() if k != key}
        kept = {k: v for k, v in current["vault"].items() if k != key}
        entry = None
        if name is not None:
            env[key] = name  # Only the name is stored; the value stays outside DevRoster.
        else:
            entry = _protect(connection_id, key, value)
            if entry is None:
                values[key] = value
            else:
                kept[key] = entry
        try:
            private_directory(path.parent)
            atomic_json(path, {"resource": resource, "values": values, "env": env, "vault": kept})
        except BaseException:
            _forget(entry)  # Nothing points at the new item.
            raise
        _forget(current["vault"].get(key))

    @staticmethod
    def revoke_shared(connection_id):
        """Remove a shared connection's private values; every binding then needs re-auth."""
        path = _shared_path(connection_id)
        try:
            kept = list((_read_shared(connection_id) or {}).get("vault", {}).values())
        except (DomainError, OSError, ValueError):
            kept = []  # An unreadable file is still removed.
        path.unlink(missing_ok=True)
        for entry in kept:
            _forget(entry)

    @staticmethod
    def protect_shared():
        """Move values still written in the files to the OS store. Counts only, no values."""
        report = {"available": os_vault.vault() is not None, "moved": 0, "left": 0}
        directory = shared_directory()
        if not report["available"] or not directory.is_dir() or directory.is_symlink():
            return report
        for path in sorted(directory.glob("*.json")):
            if not IDENTIFIER.fullmatch(path.stem):
                continue
            row = _read_shared(path.stem) or {}
            for key, value in sorted(row.get("values", {}).items()):
                CredentialStore.save_shared(path.stem, key, value, resource=row.get("resource", ""))
                moved = key in (_read_shared(path.stem) or {}).get("vault", {})
                report["moved" if moved else "left"] += 1
        return report

    @staticmethod
    def storage_report():
        """How many shared values are kept where (no names, no values)."""
        report = {"available": os_vault.vault() is not None, "file": 0, "protected": 0, "unreadable": 0}
        directory = shared_directory()
        if not directory.is_dir() or directory.is_symlink():
            return report
        for path in sorted(directory.glob("*.json")):
            row = (_read_shared(path.stem) if IDENTIFIER.fullmatch(path.stem) else None) or {}
            report["file"] += len(row.get("values", {}))
            for entry in row.get("vault", {}).values():
                try:
                    _reveal(entry)
                    report["protected"] += 1
                except os_vault.VaultError:
                    report["unreadable"] += 1
        return report

    @staticmethod
    def shared_entry(connection_id) -> dict:
        """``{KEY: value}`` of one shared connection, for process environments only."""
        return _resolved(_read_shared(connection_id) or {})

    @staticmethod
    def shared_sources(connection_id) -> dict:
        """``{KEY: {"env", "present", "hint"}}`` for keys kept as variable names (no values)."""
        row = _read_shared(connection_id) or {}
        result = {}
        for key, name in row.get("env", {}).items():
            present = bool(os.environ.get(name))
            result[key] = {"env": name, "present": present, "hint": "" if present else missing_hint(name)}
        return result

    @staticmethod
    def remove_shared_key(connection_id, key):
        """Delete one private value of a shared connection; the others stay."""
        validate_key(key)
        path = _shared_path(connection_id)
        current = _read_shared(connection_id)
        if current is None or not any(key in current[part] for part in ("values", "env", "vault")):
            return
        values = {k: v for k, v in current["values"].items() if k != key}
        env = {k: v for k, v in current.get("env", {}).items() if k != key}
        kept = {k: v for k, v in current["vault"].items() if k != key}
        atomic_json(path, dict(current, values=values, env=env, vault=kept))
        _forget(current["vault"].get(key))

    @staticmethod
    def shared_values():
        """All shared private values on this device, for redaction only."""
        directory = shared_directory()
        if not directory.is_dir() or directory.is_symlink():
            return ()
        values = []
        for path in sorted(directory.glob("*.json")):
            row = _read_shared(path.stem) if IDENTIFIER.fullmatch(path.stem) else None
            values.extend(v for v in _resolved(row or {}).values() if v)
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
            values = _resolved(row or {})
            if row is None or not values.get(parts[2]):
                name = (row or {}).get("env", {}).get(parts[2])
                if name:
                    denied(f"공통 연결 인증을 읽을 환경변수 {name}이(가) 설정되지 않았습니다.")
                if parts[2] in (row or {}).get("vault", {}):
                    denied(UNREADABLE)
                denied("공통 연결 인증이 없거나 해지되었습니다. 다시 인증해주세요.")
            if row.get("resource") and row["resource"] != resource:
                denied("공통 인증이 묶인 주소와 연결 주소가 다릅니다.", ErrorCode.POLICY_VIOLATION)
            return {parts[2]: values[parts[2]]}
        denied("인증 참조 형식을 확인해주세요.", ErrorCode.INVALID_INPUT)


def permission_report(repair=False, extra=()):
    """Credential files readable by others (POSIX). With repair, tighten them to 0700/0600.

    Copies, restores and sync tools can loosen the modes DevRoster wrote; Windows relies on
    the user profile ACL and is reported as not applicable.
    """
    if os.name == "nt":
        return {"applicable": False, "loose": [], "repaired": []}
    targets = []
    root = management_root() / "credentials"
    if root.is_dir() and not root.is_symlink():
        targets.append((root, 0o700))
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                continue
            targets.append((path, 0o700 if path.is_dir() else 0o600))
    targets += [(path, 0o600) for path in map(Path, extra) if path.is_file() and not path.is_symlink()]
    loose, repaired = [], []
    for path, mode in targets:
        current = stat.S_IMODE(path.stat().st_mode)
        if current & 0o077:
            loose.append({"path": str(path), "mode": oct(current), "expected": oct(mode)})
            if repair:
                path.chmod(mode)
                repaired.append(str(path))
    return {"applicable": True, "loose": loose, "repaired": repaired}
