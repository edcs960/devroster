"""Whether DevRoster may run what a workspace's own settings ask it to run.

A workspace whose configuration sits inside the project folder (the portable or local
layout, or an unregistered external marker there) can arrive with the repository: a
cloned ``.agent-dev`` may carry runner commands, verification commands, MCP servers and
hook approvals written by someone else. Such a workspace is trusted only when

- this machine's DevRoster created its state DB (recorded when the DB file is created), or
- the user reviewed it and trusted it here, or
- it was already registered in the app when this check was introduced (first use seeds
  the registered workspaces, so upgrading changes nothing for existing projects).

Every decision lives in the management folder, never in the project, so a repository
cannot ship its own trust. A decision names the DB file by path and file identity
(device and inode): a different folder cloned or restored to the same path later is a
different file and starts untrusted. Workspaces whose configuration DevRoster keeps outside the
project (a registered external mapping, the legacy engine folder) need no trust.
Hook approvals live here too, keyed by the same state DB.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_dev.core.io import locked
from agent_dev.core.storage_locations import EXTERNAL, LEGACY, management_root
from agent_dev.domain.errors import DomainError, ErrorCode

TRUST_FILE = "workspace-trust.json"
VERSION = 1


def _path() -> Path:
    return management_root() / TRUST_FILE


def _read() -> dict:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A damaged file trusts nothing new; it never silently trusts everything.
        return {"version": VERSION, "databases": {}, "hooks": {}, "damaged": True}
    return value if isinstance(value, dict) else {}


def _write(value: dict) -> None:
    from agent_dev.adapters.project_setup import atomic_json

    atomic_json(_path(), dict(value, version=VERSION))


def _key(path) -> str:
    return str(Path(path).expanduser().resolve())


def _identity(path) -> list[int] | None:
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return [info.st_dev, info.st_ino]


def _entry(origin: str, database, **extra) -> dict:
    return {"origin": origin, "file": _identity(database), "at": time.time(), **extra}


def _matches(entry, database) -> bool:
    return isinstance(entry, dict) and entry.get("file") is not None and entry.get("file") == _identity(database)


def locations_for(root):
    from agent_dev.adapters.storage_registry import workspace_locations
    from agent_dev.adapters.workspace import configuration_root

    return workspace_locations(configuration_root(Path(root)))


def needs_trust(locations) -> bool:
    """Only configuration that can travel with the project folder needs a decision."""
    # The legacy engine folder and a registered mapping are folders the user chose.
    registered = locations.layout == EXTERNAL and bool(locations.workspace_id)
    return locations.layout != LEGACY and not registered


def _seeded(value: dict) -> dict:
    """First use: trust the workspaces the app already knew, so upgrading changes nothing."""
    if "databases" in value:
        return value
    value = {"databases": {}, "hooks": {}}
    from agent_dev.adapters.workspace_registry import roots

    try:
        known = roots()
    except (OSError, TypeError, ValueError):
        known = []
    now = time.time()
    for root in known:
        try:
            database = locations_for(root).state_db
        except (DomainError, OSError, ValueError):
            continue
        if database.is_file():
            value["databases"][_key(database)] = dict(_entry("registered", database), at=now)
    return value


def _update(change) -> dict:
    with locked(_path()):
        value = _seeded(_read())
        value.setdefault("databases", {})
        value.setdefault("hooks", {})
        change(value)
        _write(value)
        return value


def ensure_seeded() -> None:
    """Seed from the registry before this run registers anything new.

    Entry points call this first, so a folder opened for the first time right after an
    upgrade (``devroster app --root <clone>``) is not mistaken for an already-known one.
    """
    try:
        if "databases" not in _read():
            _update(lambda _value: None)
    except (DomainError, OSError, ValueError, TypeError):
        pass  # Without a trust file every in-folder workspace asks for trust.


def record_created(database) -> None:
    """Called when DevRoster creates a state DB: what this machine made is trusted."""
    key = _key(database)

    def change(value):
        # A new file at a known path replaces whatever was decided about the old one.
        value["databases"][key] = _entry("created", database)
        value["hooks"].pop(key, None)

    _update(change)


def status(root) -> dict:
    locations = locations_for(root)
    result = {
        "needs_trust": needs_trust(locations),
        "trusted": True,
        "config_root": str(locations.config_root),
        "source_root": str(locations.source_root),
    }
    if not result["needs_trust"]:
        return result
    database = locations.state_db
    if not database.is_file():
        return result  # Nothing came with the folder yet; DevRoster will create it.
    value = _read()
    if "databases" not in value:
        value = _update(lambda _value: None)  # Seed once, then decide.
    result["trusted"] = _matches(value.get("databases", {}).get(_key(database)), database)
    return result


def is_trusted(root) -> bool:
    try:
        return status(root)["trusted"]
    except (DomainError, OSError, ValueError):
        return False


def require(root, action: str) -> None:
    if not is_trusted(root):
        raise DomainError(
            ErrorCode.APPROVAL_REQUIRED,
            f"이 프로젝트 폴더의 DevRoster 설정은 이 컴퓨터에서 만든 것이 아닙니다. {action} 전에 "
            "프로젝트 설정 화면의 '설정 신뢰'에서 실행될 명령을 확인하고 신뢰해주세요.",
        )


def trust(root, actor: str = "local-user") -> dict:
    """The user reviewed this folder's settings and trusts them on this machine."""
    locations = locations_for(root)
    if not needs_trust(locations) or not locations.state_db.is_file():
        return status(root)
    key = _key(locations.state_db)

    def change(value):
        value["databases"][key] = _entry("trusted", locations.state_db, actor=str(actor)[:80])

    _update(change)
    return status(root)


def forget(database) -> None:
    """Drop every decision about a state DB (a restored or replaced database)."""
    key = _key(database)

    def change(value):
        value["databases"].pop(key, None)
        value["hooks"].pop(key, None)

    _update(change)


def hook_approval(config_root) -> str:
    """The hook folder digest the user approved for this workspace, or ''."""
    try:
        database = locations_for(config_root).state_db
    except (DomainError, OSError, ValueError):
        return ""
    row = _read().get("hooks", {}).get(_key(database))
    return row.get("digest", "") if _matches(row, database) else ""


def approve_hooks(config_root, digest_value: str, actor: str) -> None:
    database = locations_for(config_root).state_db
    if not database.is_file():
        # The approval names the DB file; make sure it exists (and is recorded as ours).
        from agent_dev.adapters.state.sqlite.database import Database

        Database(database)
    key = _key(database)

    def change(value):
        value["hooks"][key] = _entry("hooks", database, digest=digest_value, actor=str(actor)[:80])

    _update(change)


def _argv(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(part) for part in value] if isinstance(value, list) else []


def review(root) -> dict:
    """What DevRoster would run from this folder's settings, for the user to read before trusting."""
    result = status(root)
    if not result["needs_trust"] or result["trusted"]:
        return dict(result, items=[])
    from agent_dev.adapters import ai_hooks
    from agent_dev.adapters.project_setup import ProjectSetup

    items = []
    try:
        settings = ProjectSetup(Path(root)).read()
    except (DomainError, OSError, ValueError, TypeError, KeyError):
        settings = {}
    for provider, row in (settings.get("ai", {}).get("runners", {}) or {}).items():
        if isinstance(row, dict) and row.get("enabled") and row.get("command"):
            items.append({"kind": "AI 도구 실행파일", "name": provider, "command": _argv(row["command"])})
    for row in settings.get("verification", []) or []:
        items.append({"kind": "검증 명령", "name": str(row.get("name", "")), "command": _argv(row.get("command"))})
    for row in settings.get("preview_services", []) or []:
        items.append({"kind": "미리보기 서비스", "name": str(row.get("id", "")), "command": _argv(row.get("command"))})
    try:
        from agent_dev.adapters.integrations import Integrations

        for row in Integrations(Path(root)).list():
            if row.get("transport") == "stdio" and not row.get("shared"):
                items.append({"kind": "MCP 서버", "name": str(row.get("name") or row.get("id")), "command": _argv(row.get("command"))})
    except (DomainError, OSError, ValueError, TypeError, KeyError):
        pass
    try:
        hooks = ai_hooks.read_hooks(Path(result["source_root"]))["events"]
    except (DomainError, OSError, ValueError):
        hooks = {}
    for event, rows in hooks.items():
        for row in rows:
            items.append({"kind": f"훅 ({event}, 별도 승인 필요)", "name": row["name"], "command": row["run"]})
    return dict(result, items=items)
