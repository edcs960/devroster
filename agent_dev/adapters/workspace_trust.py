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
(device, inode and, on Linux, birth time): a different folder cloned or restored to the
same path later is a different file and starts untrusted. Linux filesystems hand a freed
inode number to the next new file, so device and inode alone would accept a replaced
file there; see ``_identity``. Workspaces whose configuration DevRoster keeps outside the
project (a registered external mapping, the legacy engine folder) need no trust.
Hook approvals live here too, keyed by the same state DB, and so do the confirmations of
stdio MCP launch commands (one per connection, naming the exact command the user saw).
"""

from __future__ import annotations

import ctypes
import functools
import json
import os
import sys
import time
from pathlib import Path

from agent_dev.core.io import atomic_json, locked
from agent_dev.core.storage_locations import management_root
from agent_dev.domain.errors import DomainError, ErrorCode

TRUST_FILE = "workspace-trust.json"
# 2: a file identity also carries the birth time where the system reports one.
VERSION = 2
SECTIONS = ("databases", "hooks", "commands")


def _path() -> Path:
    return management_root() / TRUST_FILE


def _outdated(value: dict) -> bool:
    version = value.get("version", 1)
    return type(version) is not int or version < VERSION


def _read() -> dict:
    """The trust file with every decision in the current identity format.

    A version 1 file named each DB by device and inode only. An entry that still names
    the file at its path is read with that file's full identity; the next write (entry
    points make one through ``ensure_seeded``) stores it. From then on an old-style
    entry cannot match a file whose system reports a birth time.
    """
    value = _load()
    if not _outdated(value):
        return value
    for section in SECTIONS:
        rows = value.get(section)
        for key, row in rows.items() if isinstance(rows, dict) else ():
            known = row.get("file") if isinstance(row, dict) else None
            current = _identity(key) if isinstance(known, list) and len(known) == 2 else None
            if current is not None and current[:2] == known:
                row["file"] = current
    return value


def _load() -> dict:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A damaged file trusts nothing new; it never silently trusts everything.
        return {"version": VERSION, "databases": {}, "hooks": {}, "commands": {}, "damaged": True}
    return value if isinstance(value, dict) else {}


def _write(value: dict) -> None:
    atomic_json(_path(), dict(value, version=VERSION))


def _key(path) -> str:
    return str(Path(path).expanduser().resolve())


@functools.cache
def _statx():
    """libc's statx on Linux, or None (another system, or a libc without it)."""
    if sys.platform != "linux":
        return None
    try:
        call = ctypes.CDLL(None, use_errno=True).statx
    except (OSError, AttributeError):
        return None
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_char_p]
    call.restype = ctypes.c_int
    return call


def _born(path) -> tuple[int, int] | None:
    """(inode, birth time in ns) of a file on Linux, when the filesystem records one.

    Asked by path: opening the DB file here and closing it again would drop the POSIX
    locks SQLite holds on it in this process, which is why the inode generation
    (``FS_IOC_GETVERSION``, needs an open file) is not used.
    """
    call = _statx()
    if call is None:
        return None
    at_fdcwd, inode_field, btime = -100, 0x100, 0x800
    buffer = ctypes.create_string_buffer(256)  # struct statx
    if call(at_fdcwd, os.fsencode(path), 0, inode_field | btime, buffer) != 0:
        return None
    raw = buffer.raw
    if not int.from_bytes(raw[0:4], sys.byteorder) & btime:  # stx_mask
        return None
    inode = int.from_bytes(raw[32:40], sys.byteorder)  # stx_ino
    seconds = int.from_bytes(raw[80:88], sys.byteorder, signed=True)  # stx_btime
    return inode, seconds * 1_000_000_000 + int.from_bytes(raw[88:92], sys.byteorder)


def _identity(path) -> list[int] | None:
    """What tells this file from any other file that is ever at this path.

    Device and inode are enough on macOS and Windows, which do not hand a freed file
    number to a new file. Linux filesystems (ext4, XFS, Btrfs, overlayfs) do, so the
    birth time is added there. A filesystem that reports none (some network and FUSE
    mounts, ext4 with 128-byte inodes) keeps device and inode only.
    """
    try:
        info = Path(path).stat()
    except OSError:
        return None
    born = _born(path)
    if born is None or born[0] != info.st_ino:
        # No birth time, or the file changed between the two calls: the shorter identity
        # never matches a decision that recorded a birth time.
        return [info.st_dev, info.st_ino]
    return [info.st_dev, info.st_ino, born[1]]


def _entry(origin: str, database, **extra) -> dict:
    return {"origin": origin, "file": _identity(database), "at": time.time(), **extra}


def _matches(entry, database) -> bool:
    return isinstance(entry, dict) and entry.get("file") is not None and entry.get("file") == _identity(database)


def locations_for(root):
    from agent_dev.adapters.workspace_locator import configuration_root, workspace_locations

    return workspace_locations(configuration_root(Path(root)))


def needs_trust(locations) -> bool:
    """Only configuration that can travel with the project folder needs a decision."""
    return locations.needs_trust


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
        value.setdefault("commands", {})
        change(value)
        _write(value)
        return value


def ensure_seeded() -> None:
    """Seed from the registry before this run registers anything new.

    Entry points call this first, so a folder opened for the first time right after an
    upgrade (``devroster app --root <clone>``) is not mistaken for an already-known one.
    """
    try:
        value = _read()
        if "databases" not in value or _outdated(value):
            _update(lambda _value: None)  # Also stores identities read from a version 1 file.
    except (DomainError, OSError, ValueError, TypeError):
        pass  # Without a trust file every in-folder workspace asks for trust.


def record_created(database) -> None:
    """Called when DevRoster creates a state DB: what this machine made is trusted."""
    key = _key(database)

    def change(value):
        # A new file at a known path replaces whatever was decided about the old one.
        value["databases"][key] = _entry("created", database)
        value["hooks"].pop(key, None)
        value["commands"].pop(key, None)

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
        value["commands"].pop(key, None)

    _update(change)


def command_approval(config_root, name: str) -> str:
    """The launch digest the user confirmed for one stdio connection here, or ''."""
    try:
        database = locations_for(config_root).state_db
    except (DomainError, OSError, ValueError):
        return ""
    row = _read().get("commands", {}).get(_key(database))
    if not _matches(row, database):
        return ""
    item = row.get("items", {}).get(name)
    return item.get("digest", "") if isinstance(item, dict) else ""


def approve_command(config_root, name: str, digest_value: str, actor: str) -> None:
    database = locations_for(config_root).state_db
    if not database.is_file():
        from agent_dev.adapters.state.sqlite.database import Database

        Database(database)
    key = _key(database)

    def change(value):
        row = value["commands"].get(key)
        if not _matches(row, database):
            row = _entry("commands", database, items={})  # another file: earlier answers do not carry over
        row.setdefault("items", {})[name] = {"digest": digest_value, "actor": str(actor)[:80], "at": time.time()}
        value["commands"][key] = row

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
