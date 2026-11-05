"""Copy -> verify -> switch moves of one workspace's settings or of the whole management root.

Scopes never mix:

- ``workspace``: copy a project-local, legacy or external configuration (canonical/Studio
  DB, config/starter/data, roles, caches, private environment/OAuth) into a new folder and
  publish it through the workspace registry. Source, worktrees and output stay in place.
- ``management``: copy the management root (registry, global settings, external project
  folders under ``projects/``, credentials, app CLI homes) and switch the bootstrap pointer.

Journal states: ``planned -> copying -> verified -> switched -> completed``; failures are
``failed`` or ``rollback_required``; a reversed pointer ends ``rolled_back``. Originals are
never deleted. SQLite files are copied with the backup API, never as raw WAL files. The
journal and plan carry paths, counts and digests only; never secret values.
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import NoReturn

from agent_dev.adapters import storage_registry
from agent_dev.adapters.project_setup import atomic_json, private_directory
from agent_dev.adapters.storage_registry import workspace_locations
from agent_dev.adapters.workspace import configuration_root, metadata
from agent_dev.core.storage_locations import (
    BOOTSTRAP_VERSION,
    EXTERNAL,
    HOME_OVERRIDE,
    RUNTIME_DIR,
    bootstrap_file,
    management_root,
)
from agent_dev.domain.errors import DomainError, ErrorCode

SCOPES = ("workspace", "management")
ACTIONS = ("resume", "rollback")
MIGRATION_ID = re.compile(r"[a-f0-9]{32}")
CONFIG_DIRS = ("config", "starter", "data")
CACHE_DIRS = ("design-references", "integration-artifacts", "source-graphs")
SKIPPED = {"process_registry.json"}
LOCATION_KEYS = ("workspace_id", "source_root", "runtime_root")


def invalid(message, code=ErrorCode.INVALID_INPUT) -> NoReturn:
    raise DomainError(code, message)


# -- journal and maintenance lock -----------------------------------------------------


def migrations_root():
    return management_root() / "migrations"


def maintenance_lock_path():
    return migrations_root() / "maintenance.lock"


def _journal_file(migration_id, root=None):
    if not isinstance(migration_id, str) or not MIGRATION_ID.fullmatch(migration_id):
        invalid("이전 ID를 확인해주세요.")
    return (Path(root) if root else migrations_root()) / migration_id / "journal.json"


def journal(migration_id) -> dict:
    path = _journal_file(migration_id)
    if not path.is_file():
        invalid("이전 기록을 찾을 수 없습니다.")
    return json.loads(path.read_text(encoding="utf-8"))


def _save(record):
    record["updated_at"] = time.time()
    for root in record["journal_roots"]:
        path = _journal_file(record["plan_id"], root)
        private_directory(path.parent)
        atomic_json(path, record)


def _state(record, state, **changes):
    record.update(changes, state=state)
    record.setdefault("history", []).append({"state": state, "at": time.time()})
    _save(record)


@contextlib.contextmanager
def _maintenance():
    """One move or recovery at a time per management root (AC-10)."""
    from agent_dev.storage.process_registry import is_process_alive

    path = maintenance_lock_path()
    private_directory(path.parent)
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                holder = json.loads(path.read_text(encoding="utf-8")).get("pid")
            except (OSError, ValueError, AttributeError):
                holder = None
            if isinstance(holder, int) and is_process_alive(holder):
                invalid("다른 저장 위치 이전이 진행 중입니다.", ErrorCode.LEASE_CONFLICT)
            path.unlink(missing_ok=True)  # The recorded holder has exited.
    else:
        invalid("저장 위치 이전 잠금을 얻지 못했습니다.", ErrorCode.LEASE_CONFLICT)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump({"pid": os.getpid(), "created_at": time.time()}, output)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


# -- shared helpers -------------------------------------------------------------------


def _check(checks, name, ok, message, fix=""):
    checks.append(
        {"name": name, "status": "pass" if ok else "fail", "message": message, "fix": fix}
    )
    return ok


def _digest_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sqlite_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        contextlib.closing(
            sqlite3.connect(Path(source).as_uri() + "?mode=ro", uri=True)
        ) as original,
        contextlib.closing(sqlite3.connect(target)) as copy,
    ):
        original.backup(copy)
    target.chmod(0o600)


def _table_digests(path):
    """Row-by-row digest of every table, for copy verification."""
    result = {}
    with contextlib.closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as sql:
        if (
            sql.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or sql.execute("PRAGMA foreign_key_check").fetchone()
        ):
            return None
        tables = [
            r[0]
            for r in sql.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        for table in tables:
            hasher, count = hashlib.sha256(), 0
            for row in sql.execute(f'SELECT * FROM "{table}" ORDER BY 1'):
                hasher.update(repr(tuple(row)).encode())
                count += 1
            result[table] = [count, hasher.hexdigest()]
    return result


def _destination_checks(checks, destination, protected):
    if not destination.is_absolute():
        invalid("새 위치는 절대 경로여야 합니다.")
    _check(
        checks,
        "destination_new",
        not destination.exists() and not destination.is_symlink(),
        "새 위치는 아직 없는 폴더여야 합니다.",
        "비어 있는 새 경로를 선택해주세요. 기존 폴더는 덮어쓰지 않습니다.",
    )
    parent = destination.parent
    # The management root's projects/ folder is created on apply when it does not exist yet.
    if parent == management_root().resolve() / "projects" and not parent.exists():
        parent = parent.parent
    _check(
        checks,
        "destination_parent",
        parent.is_dir() and not parent.is_symlink() and os.access(parent, os.W_OK),
        "새 위치의 상위 폴더가 있고 쓸 수 있어야 합니다.",
        "상위 폴더 권한을 확인해주세요.",
    )
    overlaps = [str(p) for p in protected if storage_registry._overlaps(destination, Path(p))]
    _check(
        checks,
        "destination_overlap",
        not overlaps,
        "새 위치가 기존 위치와 겹치지 않아야 합니다." + (f" 겹침: {overlaps}" if overlaps else ""),
        "소스·현재 설정·관리 폴더 밖의 경로를 선택해주세요.",
    )


def _disk_check(checks, destination, needed):
    existing = next(p for p in destination.parents if p.exists())
    try:
        free = shutil.disk_usage(existing).free
    except OSError:
        free = 0
    _check(
        checks,
        "disk",
        free >= needed * 2 + 1024 * 1024,
        f"필요 공간 약 {needed * 2} bytes, 사용 가능 {free} bytes.",
        "저장 공간을 확보한 뒤 다시 점검해주세요.",
    )


def _active_processes(paths):
    from agent_dev.storage.process_registry import is_process_alive

    alive = []
    for path in paths:
        if not Path(path).is_file():
            continue
        try:
            rows = json.loads(Path(path).read_text(encoding="utf-8")).get("processes", [])
        except (OSError, ValueError, AttributeError):
            rows = []
        alive.extend(
            row for row in rows if isinstance(row.get("pid"), int) and is_process_alive(row["pid"])
        )
    return alive


def _quiet(checks, name, path, kind):
    from agent_dev.adapters.backup import _quiescent

    if not Path(path).is_file():
        return
    try:
        with contextlib.closing(
            sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)
        ) as sql:
            _quiescent(sql, kind)
        _check(checks, name, True, "실행 중인 작업·lease가 없습니다.")
    except DomainError:
        _check(
            checks,
            name,
            False,
            "실행 중인 작업, 작업 잠금 또는 Studio 작업이 있습니다.",
            "진행 중인 작업을 끝내거나 복구한 뒤 다시 점검해주세요.",
        )


def _tree_files(base, *, suffixes=None, skipped=()):
    base = Path(base)
    if base.is_symlink():
        invalid("복사할 폴더에 심볼릭 링크를 사용할 수 없습니다: " + str(base))
    if not base.is_dir():
        return []
    files = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            invalid("복사할 파일에 심볼릭 링크를 사용할 수 없습니다: " + str(path))
        if (
            path.is_file()
            and path.name not in skipped
            and (suffixes is None or path.suffix in suffixes)
        ):
            files.append(path)
    return files


# -- workspace scope ------------------------------------------------------------------


def _workspace_files(locations):
    """(source file, relative target, private) for everything copied besides databases."""
    config = locations.config_root
    files = []
    for directory in CONFIG_DIRS:
        for path in _tree_files(config / directory, suffixes={".json", ".md"}, skipped=SKIPPED):
            files.append((path, path.relative_to(config).as_posix(), False))
    if (config / "README.md").is_file():
        files.append((config / "README.md", "README.md", False))
    for path in _tree_files(locations.role_dir, suffixes={".md"}):
        files.append((path, "roles/" + path.relative_to(locations.role_dir).as_posix(), False))
    for directory in CACHE_DIRS:
        for path in _tree_files(locations.cache_root / directory):
            relative = path.relative_to(locations.cache_root).as_posix()
            files.append((path, "cache/" + relative, False))
    if locations.layout != EXTERNAL:
        if locations.environment_file.is_file():
            files.append((locations.environment_file, "environment.json", True))
        for path in _tree_files(locations.oauth_dir, suffixes={".json"}):
            files.append((path, "oauth/" + path.name, True))
    return files


def _plan_workspace(source, destination):
    config = configuration_root(Path(source))
    locations = workspace_locations(config)
    checks = []
    initialized = _check(
        checks,
        "workspace",
        locations.state_db.is_file(),
        f"{locations.layout} 레이아웃, 저장소 {locations.state_db}",
        "프로젝트 설정을 먼저 저장해 workspace를 준비해주세요.",
    )
    _destination_checks(
        checks,
        destination,
        [locations.source_root, locations.config_root, locations.runtime_root],
    )
    if not initialized:
        return {"checks": checks, "bytes": 0}
    from agent_dev.adapters.backup import validate_database

    try:
        version = validate_database(locations.state_db)
        _check(checks, "schema", True, f"canonical schema v{version}")
    except DomainError as error:
        _check(checks, "schema", False, str(error), "지원하는 버전으로 DB를 먼저 복구해주세요.")
    _quiet(checks, "canonical_idle", locations.state_db, "canonical")
    _quiet(checks, "studio_idle", locations.studio_db, "studio")
    running = _active_processes(
        [
            locations.state_db.parent / "data/process_registry.json",
            locations.config_root / "data/process_registry.json",
        ]
    )
    _check(
        checks,
        "processes",
        not running,
        f"실행 중인 CLI 프로세스 {len(running)}개",
        "실행 중인 작업을 끝낸 뒤 다시 점검해주세요.",
    )
    _snapshot_paths(checks, locations, locations.state_db)
    workspace_id = locations.workspace_id or uuid.uuid4().hex
    _mapping_check(checks, locations.source_root, destination, locations.runtime_root, workspace_id)
    files = _workspace_files(locations)
    databases = [p for p in (locations.state_db, locations.studio_db) if p.is_file()]
    needed = sum(p.stat().st_size for p in databases) + sum(p.stat().st_size for p, _, _ in files)
    _disk_check(checks, destination, needed)
    return {
        "workspace_id": workspace_id,
        "source_layout": locations.layout,
        "config_root": str(locations.config_root),
        "source_root": str(locations.source_root),
        "runtime_root": str(locations.runtime_root),
        "original_databases": [str(p) for p in databases],
        "checks": checks,
        "bytes": needed,
    }


def _mapping_check(checks, source, destination, runtime, workspace_id):
    """Every target the registry switch would refuse is refused before anything is copied."""
    try:
        storage_registry.check_mapping(source, destination, runtime, workspace_id)
        _check(checks, "registry_mapping", True, "새 위치를 프로젝트 매핑에 등록할 수 있습니다.")
    except DomainError as error:
        _check(
            checks,
            "registry_mapping",
            False,
            str(error),
            "관리 폴더의 projects/ 아래 또는 소스·다른 프로젝트와 겹치지 않는 경로를 선택해주세요.",
        )


def _snapshot_paths(checks, locations, database):
    unresolved = []
    with contextlib.closing(sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)) as sql:
        for snapshot_id, resolved in sql.execute("SELECT id,resolved_json FROM input_snapshots"):
            for binding in json.loads(resolved).get("repositories", {}).values():
                worktree = binding.get("worktree")
                if worktree and not Path(worktree).resolve().is_relative_to(
                    locations.worktree_root
                ):
                    unresolved.append(snapshot_id)
    _check(
        checks,
        "snapshot_paths",
        not unresolved,
        "승인 snapshot의 worktree가 현재 실행 위치 안에 있습니다."
        if not unresolved
        else f"해석할 수 없는 snapshot 경로 {len(set(unresolved))}개: 이전을 보류합니다.",
        "다른 위치에서 만든 snapshot은 digest를 고치지 않습니다. 해당 작업을 정리한 뒤 이전해주세요.",
    )


def _credentials_target(record):
    return management_root() / "credentials" / "projects" / record["workspace_id"]


def _staging(record):
    destination = Path(record["destination"])
    return destination.parent / f".{destination.name}.staging-{record['plan_id']}"


def _copy_workspace(record, staging):
    locations = workspace_locations(Path(record["config_root"]))
    staging.mkdir(mode=0o700)
    _sqlite_copy(locations.state_db, staging / "state.db")
    if locations.studio_db.is_file():
        _sqlite_copy(locations.studio_db, staging / "studio/studio.db")
    private = staging / ".credentials"
    for source, relative, secret in _workspace_files(locations):
        target = (private if secret else staging) / relative
        if secret:
            private_directory(target.parent)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o600)
    from agent_dev.adapters.project_setup import ProjectSetup

    setup = ProjectSetup(locations.config_root)
    marker = metadata(locations.config_root) or {
        "format_version": 1,
        "project_id": setup.current_profile()[0],
        "name": setup.read()["name"],
        "engine_version": "0.3.0",
        "preset": "generic",
    }
    marker = dict(
        marker,
        workspace_id=record["workspace_id"],
        source_root=record["source_root"],
        runtime_root=record["runtime_root"],
    )
    atomic_json(staging / "workspace.json", marker)
    (staging / ".gitignore").write_text("*\n", encoding="utf-8")


def _verify_workspace(record, staging):
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository
    from agent_dev.migration.legacy import collect_sources, compare

    locations = workspace_locations(Path(record["config_root"]))
    checks = []
    pairs = [("canonical", locations.state_db, staging / "state.db")]
    if locations.studio_db.is_file():
        pairs.append(("studio", locations.studio_db, staging / "studio/studio.db"))
    for kind, source, copy in pairs:
        original, copied = _table_digests(source), _table_digests(copy)
        _check(
            checks,
            "database:" + kind,
            original is not None and original == copied,
            "integrity·foreign key·테이블별 행 digest 일치"
            + (f" ({sum(v[0] for v in copied.values())} rows)" if copied else ""),
        )
    private = staging / ".credentials"
    mismatched = [
        relative
        for source, relative, secret in _workspace_files(locations)
        if _digest_file(source) != _digest_file((private if secret else staging) / relative)
    ]
    _check(
        checks,
        "files",
        not mismatched,
        f"설정·역할·캐시·비공개 파일 digest 불일치 {len(mismatched)}개",
    )
    original_marker = metadata(locations.config_root) or {}
    copied_marker = json.loads((staging / "workspace.json").read_text(encoding="utf-8"))
    _check(
        checks,
        "workspace_marker",
        all(copied_marker.get(k) == v for k, v in original_marker.items() if k not in LOCATION_KEYS)
        and copied_marker["workspace_id"] == record["workspace_id"],
        "workspace 표식의 업무 project_id·이름 보존",
    )
    db = Database(staging / "state.db", read_only=True)
    missing = [
        row["path"]
        for row in db.rows("SELECT path,digest FROM artifacts")
        if not (locations.output_root / row["path"]).is_file()
        or _digest_file(locations.output_root / row["path"]) != row["digest"]
    ]
    _check(checks, "artifacts", not missing, f"증거 파일 digest 불일치·누락 {len(missing)}개")

    def flags(path):
        return (
            json.loads(path.read_text(encoding="utf-8")).get("orchestrator_v2", {})
            if path.is_file()
            else None
        )

    _check(
        checks,
        "orchestrator_v2",
        flags(locations.config_root / "config/runtime.json")
        == flags(staging / "config/runtime.json"),
        "orchestrator_v2 플래그 동일",
    )
    if db.rows("SELECT 1 FROM legacy_sources LIMIT 1"):
        source_root = Path(record["source_root"])
        before = compare(
            WorkflowRepository(Database(locations.state_db, read_only=True)),
            collect_sources(source_root, locations.config_root / "data"),
        )
        after = compare(WorkflowRepository(db), collect_sources(source_root, staging / "data"))
        _check(
            checks,
            "refactor_compare",
            after == before,
            f"devroster refactor compare: zero_diff={after['zero_diff']} (원본과 동일)",
        )
    else:
        _check(checks, "refactor_compare", True, "레거시 원본 없음")
    _snapshot_paths(checks, locations, staging / "state.db")
    return checks


def _install_workspace(record, staging):
    destination = Path(record["destination"])
    private = staging / ".credentials"
    if private.exists():
        target = _credentials_target(record)
        if target.exists():
            invalid("이 workspace의 인증 저장 위치가 이미 있습니다.", ErrorCode.POLICY_VIOLATION)
        private_directory(target.parent)
        os.rename(private, target)
        record["credentials_installed"] = str(target)
    os.rename(staging, destination)


def _switch_workspace(record):
    destination = Path(record["destination"])
    if record["source_layout"] == EXTERNAL:
        storage_registry.relocate(
            record["workspace_id"], destination, expected_revision=record["registry_revision"]
        )
    else:
        source = Path(record["source_root"])
        storage_registry.register(
            source,
            destination,
            source / RUNTIME_DIR,
            workspace_id=record["workspace_id"],
            expected_revision=record["registry_revision"],
        )
    return [p for p in (destination / "state.db", destination / "studio/studio.db") if p.is_file()]


def _reverse_workspace(record):
    revision = storage_registry.read()["revision"]
    if record["source_layout"] == EXTERNAL:
        storage_registry.relocate(record["workspace_id"], Path(record["config_root"]), revision)
    else:
        storage_registry.unregister(record["workspace_id"], revision)


# -- management scope -----------------------------------------------------------------


def _management_skip(record):
    def ignore(directory, names):
        skipped = set()
        for name in names:
            path = Path(directory) / name
            if (
                name.endswith((".lock", "-wal", "-shm", ".db"))
                or name == "bootstrap.json"
                or path == Path(record["source"]) / "migrations" / record["plan_id"]
            ):
                skipped.add(name)
        return skipped

    return ignore


def _plan_management(source, destination):
    root = Path(source).expanduser().resolve()
    checks = []
    _check(
        checks,
        "environment_override",
        not os.environ.get(HOME_OVERRIDE),
        "DEVROSTER_HOME이 지정되어 있지 않습니다."
        if not os.environ.get(HOME_OVERRIDE)
        else "DEVROSTER_HOME이 관리 폴더를 지정하고 있어 위치 파일 전환이 적용되지 않습니다.",
        "DEVROSTER_HOME을 해제하고 앱을 다시 연 뒤 이전해주세요.",
    )
    _check(
        checks,
        "current_root",
        root == management_root().resolve(),
        "이전 원본은 현재 관리 폴더여야 합니다.",
    )
    _destination_checks(checks, destination, [root])
    registry = storage_registry.read()
    for workspace_id, row in registry["workspaces"].items():
        locations = workspace_locations(Path(row["config_root"]))
        _quiet(checks, f"idle:{workspace_id}", locations.state_db, "canonical")
        _quiet(checks, f"studio_idle:{workspace_id}", locations.studio_db, "studio")
    running = _active_processes(
        [root / "data/process_registry.json"]
        + [
            Path(row["config_root"]) / "data/process_registry.json"
            for row in registry["workspaces"].values()
        ]
    )
    _check(checks, "processes", not running, f"실행 중인 CLI 프로세스 {len(running)}개")
    # Configurations inside the root move with it; nothing else may nest with the target.
    nested = [
        row[key]
        for row in registry["workspaces"].values()
        for key in ("source_root", "runtime_root", "config_root")
        if not (key == "config_root" and Path(row[key]).is_relative_to(root))
        and storage_registry._overlaps(destination, Path(row[key]))
    ]
    _check(
        checks,
        "registered_locations",
        not nested,
        "새 관리 폴더가 등록된 소스·실행·설정 위치와 겹치지 않아야 합니다."
        + (f" 겹침: {nested}" if nested else ""),
        "모든 프로젝트 소스 밖의 경로를 선택해주세요.",
    )
    outside = [
        row["config_root"]
        for row in registry["workspaces"].values()
        if not Path(row["config_root"]).is_relative_to(root)
    ]
    checks.append(
        {
            "name": "external_configurations",
            "status": "pass",
            "message": f"관리 폴더 밖의 프로젝트 설정 {len(outside)}개는 위치를 그대로 참조합니다.",
            "fix": "",
        }
    )
    needed = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    _disk_check(checks, destination, needed)
    return {"checks": checks, "bytes": needed}


def _copy_management(record, staging):
    source = Path(record["source"])
    shutil.copytree(source, staging, symlinks=True, ignore=_management_skip(record))
    for database in sorted(source.rglob("*.db")):
        if database.is_file() and not database.is_symlink():
            _sqlite_copy(database, staging / database.relative_to(source))
    state = staging / "app.json"
    if state.is_file():
        value = json.loads(state.read_text(encoding="utf-8"))
        atomic_json(state, storage_registry.rebased(value, source, Path(record["destination"])))


def _verify_management(record, staging):
    source = Path(record["source"])
    checks = []
    ignore = _management_skip(record)
    databases = [p for p in sorted(source.rglob("*.db")) if p.is_file() and not p.is_symlink()]
    mismatched = [
        str(p.relative_to(source))
        for p in databases
        if _table_digests(p) is None
        or _table_digests(p) != _table_digests(staging / p.relative_to(source))
    ]
    _check(
        checks, "databases", not mismatched, f"DB {len(databases)}개 중 불일치 {len(mismatched)}개"
    )
    files = []
    for directory, names, filenames in os.walk(source):
        skipped = ignore(directory, names + filenames)
        names[:] = [n for n in names if n not in skipped]
        for name in filenames:
            path = Path(directory) / name
            if name not in skipped and path.is_file() and not path.is_symlink():
                files.append(path)
    registry_file = source / "app.json"  # Rewritten on purpose; checked below.
    different = [
        str(p.relative_to(source))
        for p in files
        if p != registry_file and _digest_file(p) != _digest_file(staging / p.relative_to(source))
    ]
    _check(
        checks, "files", not different, f"파일 {len(files)}개 중 digest 불일치 {len(different)}개"
    )
    app = staging / "app.json"
    if (source / "app.json").is_file():
        expected = storage_registry.rebased(
            json.loads((source / "app.json").read_text(encoding="utf-8")),
            source,
            Path(record["destination"]),
        )
        _check(
            checks,
            "registry",
            json.loads(app.read_text(encoding="utf-8")) == expected,
            "프로젝트 매핑이 새 관리 폴더 기준으로 전환됨",
        )
    return checks


def _switch_management(record):
    pointer = bootstrap_file()
    record["previous_bootstrap"] = (
        pointer.read_text(encoding="utf-8") if pointer.is_file() else None
    )
    destination = Path(record["destination"])
    record["journal_roots"] = [
        str(Path(record["source"]) / "migrations"),
        str(destination / "migrations"),
    ]
    _save(record)
    atomic_json(
        pointer,
        {
            "schema_version": BOOTSTRAP_VERSION,
            "management_root": str(destination),
            "registry_id": record["plan_id"],
        },
    )
    return [p for p in destination.rglob("*.db") if p.is_file() and not p.is_symlink()]


def _reverse_management(record):
    pointer = bootstrap_file()
    if record.get("previous_bootstrap") is None:
        pointer.unlink(missing_ok=True)
    else:
        previous = json.loads(record["previous_bootstrap"])
        atomic_json(pointer, previous)


# -- state machine ----------------------------------------------------------------------


def plan_move(source: Path, destination: Path, scope: str) -> dict:
    """Read-only preflight. Records a plan journal; never touches the registry or target."""
    if scope not in SCOPES:
        invalid("이전 범위는 workspace 또는 management입니다.")
    destination = Path(destination).expanduser()
    if not destination.is_absolute():
        invalid("새 위치는 절대 경로여야 합니다.")
    destination = destination.parent.resolve() / destination.name
    details = (
        _plan_workspace(source, destination)
        if scope == "workspace"
        else _plan_management(source, destination)
    )
    record = {
        "plan_id": uuid.uuid4().hex,
        "scope": scope,
        "source": str(Path(source).expanduser().resolve()),
        "destination": str(destination),
        "registry_revision": storage_registry.read()["revision"],
        "created_at": time.time(),
        "ready": all(row["status"] == "pass" for row in details["checks"]),
        "journal_roots": [str(migrations_root())],
        "original_retained": True,
        **details,
    }
    _state(record, "planned")
    return {k: v for k, v in record.items() if k not in {"history", "journal_roots", "updated_at"}}


def _public(record):
    return {
        "migration_id": record["plan_id"],
        "scope": record["scope"],
        "state": record["state"],
        "source": record["source"],
        "destination": record["destination"],
        "checks": record.get("verification", record["checks"]),
        "restart_required": record["state"] in {"switched", "completed"},
        "original_retained": True,
        "operator_actions": [
            "웹·CLI·데스크톱 앱을 모두 종료한 뒤 다시 열어주세요.",
            "앱 전용 CLI 로그인이 유지되는지 확인해주세요(claude auth status, codex login status).",
        ]
        if record["scope"] == "management"
        else ["열려 있는 웹 화면을 다시 열어주세요. 원본 설정은 삭제되지 않았습니다."],
    }


def _pointer_switched(record):
    """Whether the registry or bootstrap pointer already names the destination.

    The journal is written after the pointer, so a crash between the two leaves the
    journal behind; recovery trusts the pointer, never the journal alone (F1).
    """
    destination = Path(record["destination"])
    if record["scope"] == "workspace":
        try:
            found = storage_registry.find(Path(record["source_root"]))
        except DomainError:
            return True  # Unreadable mapping: never treat the copy as disposable.
        return bool(found) and Path(found[1]["config_root"]) == destination
    pointer = bootstrap_file()
    if not pointer.is_file():
        return False
    try:
        return Path(json.loads(pointer.read_text(encoding="utf-8"))["management_root"]) == (
            destination
        )
    except (OSError, ValueError, KeyError, TypeError):
        return True


def _content(folder, credentials=None):
    """Row digests of every database and digests of every other file below ``folder``.

    Keys are relative so a staging copy and its installed destination compare equal.
    Journals, SQLite side files and lock files are not content.
    """
    folder = Path(folder)
    result = {}
    for base, prefix in (
        (folder, ""),
        (Path(credentials) if credentials else None, "credentials/"),
    ):
        if base is None or not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            relative = path.relative_to(base).as_posix()
            if prefix == "" and relative.startswith(".credentials/"):
                key = "credentials/" + relative.removeprefix(".credentials/")
            else:
                key = prefix + relative
            if (
                relative.startswith("migrations/")
                or relative == "bootstrap.json"
                or path.name.endswith(("-wal", "-shm", "-journal", ".lock"))
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            result[key] = _table_digests(path) if path.suffix == ".db" else _digest_file(path)
    return result


def _destination_content(record):
    destination = Path(record["destination"])
    if record["scope"] == "workspace" and record.get("credentials_installed"):
        return _content(destination, record["credentials_installed"])
    return _content(destination)


def _original_databases(record):
    if record["scope"] == "workspace":
        return [Path(p) for p in record.get("original_databases", []) if Path(p).is_file()]
    root = Path(record["source"])
    return [
        p
        for p in sorted(root.rglob("*.db"))
        if p.is_file() and not p.is_symlink() and not p.is_relative_to(root / "migrations")
    ]


def _original_content(record):
    """What the copy must equal: original databases and every copied file."""
    if record["scope"] == "management":
        return _content(Path(record["source"]))
    locations = workspace_locations(Path(record["config_root"]))
    result: dict = {"db:" + str(p): _table_digests(p) for p in _original_databases(record)}
    for source, relative, _secret in _workspace_files(locations):
        result["file:" + relative] = _digest_file(source)
    return result


@contextlib.contextmanager
def _frozen(record):
    """Hold the write lock of every original database for copy -> verify -> switch (F4b).

    Other writers wait and then fail with "database is locked" instead of committing
    into an original that is about to be replaced. Active work is re-checked under the
    lock (F4).
    """
    from agent_dev.adapters.backup import _quiescent

    connections = []
    try:
        for path in _original_databases(record):
            sql = sqlite3.connect(path, timeout=10, isolation_level=None)
            connections.append(sql)
            sql.execute("PRAGMA busy_timeout=10000")
            try:
                sql.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                invalid(
                    "원본 DB에 다른 작업이 쓰는 중입니다. 작업을 끝낸 뒤 다시 시도해주세요.",
                    ErrorCode.LEASE_CONFLICT,
                )
            kind = (
                "studio"
                if path.name == "studio.db"
                else "canonical"
                if path.name == "state.db"
                else None
            )
            if kind:
                try:
                    _quiescent(sql, kind)
                except DomainError:
                    invalid(
                        "계획 이후 실행 중인 작업·lease가 생겼습니다. 끝낸 뒤 다시 시도해주세요.",
                        ErrorCode.LEASE_CONFLICT,
                    )
        running = _active_processes(
            [Path(p).parent / "data/process_registry.json" for p in _original_databases(record)]
        )
        if running:
            invalid("실행 중인 CLI 프로세스가 있습니다.", ErrorCode.LEASE_CONFLICT)
        yield
    finally:
        for sql in connections:
            with contextlib.suppress(sqlite3.Error):
                sql.rollback()
            sql.close()


def _discard_install(record):
    """Remove this migration's own copy, only while no pointer names it (F1)."""
    if _pointer_switched(record):
        invalid("새 위치가 이미 사용 중이라 삭제하지 않습니다.", ErrorCode.POLICY_VIOLATION)
    shutil.rmtree(Path(record["destination"]), ignore_errors=True)
    installed = record.pop("credentials_installed", None)
    if installed:
        shutil.rmtree(installed, ignore_errors=True)
    record["installed"] = False


def _fail(record, error):
    _state(record, "failed", error=type(error).__name__ + ": " + str(error)[:300])


def _mapping_ready(record):
    if record["scope"] != "workspace":
        return
    storage_registry.check_mapping(
        Path(record["source_root"]),
        Path(record["destination"]),
        Path(record["runtime_root"]),
        record["workspace_id"],
    )


def _run(record):
    workspace = record["scope"] == "workspace"
    staging = _staging(record)
    if record["state"] == "verified" and record.get("installed") and _pointer_switched(record):
        # Crashed after the switch, before the journal: the installed copy is live.
        _state(record, "switched", content_at_switch=record.get("verified_content"))
    if record["state"] == "switched":
        _state(record, "completed")
    if record["state"] == "completed":
        return _public(record)
    with _frozen(record):
        original = _original_content(record)
        if record["state"] == "verified" and original != record.get("original_content"):
            # The original changed after verification: never publish the stale copy.
            if record.get("installed"):
                _discard_install(record)
            shutil.rmtree(staging, ignore_errors=True)
            _fail(
                record,
                DomainError(ErrorCode.STALE_VERSION, "원본이 검증 후 바뀌어 다시 복사합니다."),
            )
        if record["state"] in {"planned", "failed", "copying"}:
            if staging.exists():
                shutil.rmtree(staging)  # Only this migration's own unpublished copy.
            _state(record, "copying", error=None)
            if not staging.parent.exists():
                private_directory(staging.parent)  # Only the management root's projects/ folder.
            try:
                _mapping_ready(record)
                (_copy_workspace if workspace else _copy_management)(record, staging)
                checks = (_verify_workspace if workspace else _verify_management)(record, staging)
                _verify(checks)
            except (DomainError, OSError, sqlite3.Error) as error:
                # Disk full, permissions or verification: drop the unpublished copy only.
                shutil.rmtree(staging, ignore_errors=True)
                _fail(record, error)
                raise
            _state(
                record,
                "verified",
                verification=checks,
                installed=False,
                original_content=original,
                verified_content=_content(staging),
            )
        if not record.get("installed"):
            try:
                _mapping_ready(record)
                if Path(record["destination"]).exists():
                    invalid(
                        "새 위치가 이미 있습니다. 덮어쓰지 않습니다.", ErrorCode.POLICY_VIOLATION
                    )
                (_install_workspace if workspace else _install_management)(record, staging)
            except (DomainError, OSError) as error:
                shutil.rmtree(staging, ignore_errors=True)
                if not _pointer_switched(record):
                    _discard_install(record)
                _fail(record, error)
                raise
            _state(record, "verified", installed=True)
        try:
            if _original_content(record) != record["original_content"]:
                invalid(
                    "원본이 검증 후 바뀌었습니다. 다시 이어서 복사해주세요.",
                    ErrorCode.STALE_VERSION,
                )
            _switch(record)
        except (DomainError, OSError, sqlite3.Error) as error:
            if _pointer_switched(record):
                raise
            _discard_install(record)  # Nothing published: leave nothing installed (F5).
            _fail(record, error)
            raise
        _state(record, "switched", content_at_switch=_destination_content(record))
    _state(record, "completed")
    return _public(record)


def _verify(checks):
    failed = [row["name"] for row in checks if row["status"] != "pass"]
    if failed:
        raise DomainError(ErrorCode.VERIFICATION_FAILED, "복사본 검증 실패: " + ", ".join(failed))


def _install_management(record, staging):
    os.rename(staging, Path(record["destination"]))


def _switch(record):
    return (_switch_workspace if record["scope"] == "workspace" else _switch_management)(record)


def apply_move(plan_id: str, expected_registry_revision: int) -> dict:
    record = journal(plan_id)
    if record["state"] != "planned":
        invalid("이미 실행했거나 복구가 필요한 이전입니다. 복구 명령을 사용해주세요.")
    with _maintenance():
        current = storage_registry.read()["revision"]
        if (
            type(expected_registry_revision) is not int
            or expected_registry_revision != current
            or current != record["registry_revision"]
        ):
            raise DomainError(
                ErrorCode.STALE_VERSION,
                "계획 이후 프로젝트 위치가 바뀌었습니다. 다시 점검해주세요.",
            )
        source = (
            record.get("config_root", record["source"])
            if record["scope"] == "workspace"
            else record["source"]
        )
        fresh = (
            _plan_workspace(source, Path(record["destination"]))
            if record["scope"] == "workspace"
            else _plan_management(source, Path(record["destination"]))
        )
        if not record["ready"] or any(row["status"] != "pass" for row in fresh["checks"]):
            invalid(
                "사전 점검을 통과하지 못했습니다: "
                + ", ".join(r["name"] for r in fresh["checks"] if r["status"] != "pass"),
                ErrorCode.POLICY_VIOLATION,
            )
        return _run(record)


def recover_move(migration_id: str, action: str) -> dict:
    """``resume`` finishes an interrupted move; ``rollback`` reverses it when still safe."""
    if action not in ACTIONS:
        invalid("복구 동작은 resume 또는 rollback입니다.")
    record = journal(migration_id)
    with _maintenance():
        state = record["state"]
        if action == "resume":
            if state in {"rollback_required", "rolled_back"}:
                invalid(
                    "이 이전은 이어서 진행할 수 없습니다. 현재 DB를 보존하는 새 이전이 필요합니다."
                )
            if state in {"planned", "failed", "copying"} and (
                storage_registry.read()["revision"] != record["registry_revision"]
            ):
                raise DomainError(
                    ErrorCode.STALE_VERSION,
                    "계획 이후 프로젝트 위치가 바뀌었습니다. 다시 점검해주세요.",
                )
            return _run(record)
        staging = _staging(record)
        if state in {"rollback_required", "rolled_back"}:
            invalid("되돌릴 수 없는 상태입니다: " + state)
        switched = state in {"switched", "completed"} or (
            record.get("installed") and _pointer_switched(record)
        )
        if not switched:
            # Never published: remove only this migration's own copy (never the active one).
            shutil.rmtree(staging, ignore_errors=True)
            if record.get("installed"):
                _discard_install(record)
            _state(record, "rolled_back")
            return _public(record)
        baseline = record.get("content_at_switch") or record.get("verified_content")
        if baseline is None or _destination_content(record) != baseline:
            # Any write since the switch (rows, updates, settings, credentials, revocations)
            # makes the original stale (F1, F7, F8).
            _state(record, "rollback_required")
            invalid(
                "전환 후 새 위치에 쓰기·설정·인증 변경이 있었습니다. 원본으로 되돌리지 않고, "
                "현재 데이터를 보존하는 역방향 이전이 필요합니다.",
                ErrorCode.POLICY_VIOLATION,
            )
        (_reverse_workspace if record["scope"] == "workspace" else _reverse_management)(record)
        _state(record, "rolled_back")
        return _public(record)
