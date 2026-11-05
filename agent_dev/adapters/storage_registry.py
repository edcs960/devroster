"""Stable workspace IDs mapped to source/config/runtime folders in the management root.

The mapping lives in ``<management root>/app.json`` under ``workspace_registry``; the
project list kept by ``workspace_registry`` shares the file and lock. Lookups never
create folders, databases or registry files; only ``register``/``relink`` write.
"""

import json
import uuid
from pathlib import Path

from agent_dev.core.io import locked
from agent_dev.core.storage_locations import (
    LEGACY,
    PORTABLE,
    RUNTIME_DIR,
    WorkspaceLocations,
    external_locations,
    local_locations,
    location_error,
    management_root,
    unregistered_external_locations,
    valid_workspace_id,
)
from agent_dev.domain.errors import DomainError, ErrorCode

KEY = "workspace_registry"
MARKER = "workspace.json"
REGISTRY_VERSION = 1


def state_file() -> Path:
    return management_root() / "app.json"


def _read_state() -> dict:
    path = state_file()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise location_error(f"앱의 프로젝트 목록을 읽지 못했습니다: {path}") from None
    if not isinstance(value, dict):
        raise location_error(f"앱의 프로젝트 목록 형식을 확인해주세요: {path}")
    return value


def _entry(workspace_id, row) -> dict:
    valid_workspace_id(workspace_id)
    if not isinstance(row, dict) or any(
        not isinstance(row.get(key), str) or not Path(row[key]).is_absolute()
        for key in ("source_root", "config_root", "runtime_root")
    ):
        raise location_error("앱의 workspace 매핑 형식을 확인해주세요.")
    return row


def read() -> dict:
    """The registry part of app.json: ``{"revision": int, "workspaces": {id: entry}}``."""
    value = _read_state().get(KEY, {"revision": 0, "workspaces": {}})
    if (
        not isinstance(value, dict)
        or type(value.get("revision")) is not int
        or value["revision"] < 0
        or not isinstance(value.get("workspaces"), dict)
    ):
        raise location_error("앱의 workspace 매핑 형식을 확인해주세요.")
    for workspace_id, row in value["workspaces"].items():
        _entry(workspace_id, row)
    return value


def _resolved(path, label) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise location_error(f"{label}는 절대 경로여야 합니다: {path}")
    if path.is_symlink():
        raise location_error(f"{label}에 심볼릭 링크를 사용할 수 없습니다: {path}")
    return path.resolve()


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _validate(workspaces, workspace_id, source, config, runtime):
    if not source.is_dir():
        raise location_error(f"존재하는 소스 폴더를 선택해주세요: {source}")
    if config.exists() and not config.is_dir():
        raise location_error(f"설정 위치가 폴더가 아닙니다: {config}")
    if _overlaps(source, config):
        raise location_error("설정 위치는 소스 폴더와 겹칠 수 없습니다.")
    if runtime != source / RUNTIME_DIR:
        # Worktrees and output stay with the source in this first scope.
        raise location_error("실행 위치는 소스 폴더의 .agent-dev여야 합니다.")
    if _overlaps(config, management_root().resolve()) and not config.is_relative_to(
        management_root().resolve() / "projects"
    ):
        raise location_error(
            "설정 위치는 관리 폴더의 projects/ 아래 또는 관리 폴더 밖이어야 합니다."
        )
    for other_id, row in workspaces.items():
        if other_id == workspace_id:
            continue
        if Path(row["source_root"]) == source:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "다른 workspace가 이미 이 소스 폴더를 사용합니다."
            )
        if _overlaps(Path(row["config_root"]), config):
            raise DomainError(ErrorCode.POLICY_VIOLATION, "다른 workspace의 설정 위치와 겹칩니다.")
        if any(
            _overlaps(Path(row[key]), path)
            for key in ("source_root", "runtime_root")
            for path in (config, source)
        ):
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "다른 workspace의 소스·실행 위치와 겹칩니다."
            )


def check_mapping(source, config, runtime, workspace_id=None):
    """Raise exactly what register/relocate would raise, without writing anything."""
    source = _resolved(source, "소스 폴더")
    config = Path(config).expanduser()
    if not config.is_absolute():
        raise location_error(f"설정 위치는 절대 경로여야 합니다: {config}")
    config = config.parent.resolve() / config.name
    runtime = _resolved(runtime, "실행 위치")
    _validate(read()["workspaces"], workspace_id, source, config, runtime)


def _write(state, registry):
    from agent_dev.adapters.project_setup import atomic_json

    state[KEY] = dict(registry, version=REGISTRY_VERSION)
    atomic_json(state_file(), state)


def _expect(registry, expected_revision):
    if expected_revision is not None and (
        type(expected_revision) is not int or registry["revision"] != expected_revision
    ):
        raise DomainError(
            ErrorCode.STALE_VERSION,
            "다른 화면에서 프로젝트 위치가 바뀌었습니다. 다시 불러와주세요.",
        )


def register(
    source: Path, config: Path, runtime: Path, *, workspace_id=None, expected_revision=None
) -> str:
    """Map a source folder to an external configuration folder under a new workspace ID.

    ``workspace_id`` lets a verified migration publish the ID it already wrote into the
    copied marker; ``expected_revision`` makes the switch a compare-and-swap.
    """
    source = _resolved(source, "소스 폴더")
    config = _resolved(config, "설정 위치")
    runtime = _resolved(runtime, "실행 위치")
    with locked(state_file()):
        state = _read_state()
        registry = read()
        _expect(registry, expected_revision)
        workspaces = registry["workspaces"]
        workspace_id = workspace_id or uuid.uuid4().hex
        if workspace_id in workspaces:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "이미 등록된 workspace ID입니다.")
        _validate(workspaces, workspace_id, source, config, runtime)
        workspaces[workspace_id] = {
            "source_root": str(source),
            "config_root": str(config),
            "runtime_root": str(runtime),
        }
        registry["revision"] += 1
        _write(state, registry)
    return workspace_id


def relink(workspace_id: str, source: Path, expected_revision: int) -> None:
    """Point an existing workspace ID at a moved source folder; the ID and DB stay."""
    source = _resolved(source, "소스 폴더")
    with locked(state_file()):
        state = _read_state()
        registry = read()
        if type(expected_revision) is not int or registry["revision"] != expected_revision:
            raise DomainError(
                ErrorCode.STALE_VERSION,
                "다른 화면에서 프로젝트 위치가 바뀌었습니다. 다시 불러와주세요.",
            )
        row = registry["workspaces"].get(workspace_id)
        if row is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "등록된 workspace를 찾을 수 없습니다.")
        runtime = source / RUNTIME_DIR
        _validate(registry["workspaces"], workspace_id, source, Path(row["config_root"]), runtime)
        row.update(source_root=str(source), runtime_root=str(runtime))
        registry["revision"] += 1
        _write(state, registry)


def relocate(workspace_id: str, config: Path, expected_revision: int) -> None:
    """Point an existing workspace at a verified copy of its configuration folder."""
    config = _resolved(config, "설정 위치")
    with locked(state_file()):
        state = _read_state()
        registry = read()
        _expect(registry, expected_revision)
        row = registry["workspaces"].get(workspace_id)
        if row is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "등록된 workspace를 찾을 수 없습니다.")
        _validate(
            registry["workspaces"],
            workspace_id,
            Path(row["source_root"]),
            config,
            Path(row["runtime_root"]),
        )
        row["config_root"] = str(config)
        registry["revision"] += 1
        _write(state, registry)


def unregister(workspace_id: str, expected_revision: int) -> None:
    """Remove one mapping (pointer reversal); no folder, database or credential is deleted."""
    with locked(state_file()):
        state = _read_state()
        registry = read()
        _expect(registry, expected_revision)
        if registry["workspaces"].pop(workspace_id, None) is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "등록된 workspace를 찾을 수 없습니다.")
        registry["revision"] += 1
        _write(state, registry)


def rebased(state: dict, old_root: Path, new_root: Path) -> dict:
    """app.json content with configuration folders inside ``old_root`` moved to ``new_root``."""
    import copy

    value = copy.deepcopy(state)
    registry = value.get(KEY)
    for row in (registry or {}).get("workspaces", {}).values():
        config = Path(row["config_root"])
        if config.is_relative_to(old_root):
            row["config_root"] = str(new_root / config.relative_to(old_root))
    return value


def find(path: Path) -> tuple[str, dict] | None:
    """The registered workspace whose source, config or runtime folder is ``path``."""
    path = Path(path).expanduser().resolve()
    matches = [
        (workspace_id, row)
        for workspace_id, row in read()["workspaces"].items()
        if path in {Path(row[k]) for k in ("source_root", "config_root", "runtime_root")}
    ]
    if len(matches) > 1:
        raise DomainError(
            ErrorCode.POLICY_VIOLATION,
            "여러 workspace가 같은 폴더를 사용합니다. 매핑을 확인해주세요.",
        )
    return matches[0] if matches else None


def registered_config(path: Path) -> Path | None:
    found = find(path)
    return Path(found[1]["config_root"]) if found else None


def location_report(root: Path) -> dict:
    """Where the management root and the selected workspace keep their data (no secrets)."""
    import os
    from dataclasses import asdict

    from agent_dev.adapters.workspace import configuration_root
    from agent_dev.core.storage_locations import HOME_OVERRIDE, read_bootstrap

    source = (
        "environment"
        if os.environ.get(HOME_OVERRIDE)
        else "bootstrap"
        if read_bootstrap() is not None
        else "default"
    )
    locations = workspace_locations(configuration_root(Path(root)))
    return {
        "management_root": str(management_root()),
        "management_source": source,
        "registry_revision": read()["revision"],
        "workspace": {key: str(value) for key, value in asdict(locations).items()},
    }


def state_db_path(root: Path) -> Path:
    """The canonical DB of the workspace selected by ``root`` (source or config folder)."""
    from agent_dev.adapters.workspace import configuration_root

    return workspace_locations(configuration_root(Path(root))).state_db


def _marker(config: Path) -> dict | None:
    path = config / MARKER
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise location_error(f"workspace 표식 파일을 읽지 못했습니다: {path}") from None
    if not isinstance(value, dict):
        raise location_error(f"workspace 표식 파일 형식을 확인해주세요: {path}")
    return value


def _external_marker(config: Path, marker: dict) -> WorkspaceLocations:
    workspace_id = marker.get("workspace_id")
    source = marker.get("source_root")
    runtime = marker.get("runtime_root", "")
    if (
        not isinstance(workspace_id, str)
        or not workspace_id
        or not isinstance(source, str)
        or not Path(source).is_absolute()
        or not isinstance(runtime, str)
    ):
        raise location_error(f"외부 workspace 표식의 위치 정보를 확인해주세요: {config / MARKER}")
    source_path = Path(source)
    # A marker is not an identity: without a registry entry it never selects another
    # workspace's credential folder (its workspace_id is ignored for paths and checks).
    return unregistered_external_locations(
        source_path, config, Path(runtime) if runtime else source_path / RUNTIME_DIR
    )


def workspace_locations(root: Path) -> WorkspaceLocations:
    """Resolve and validate where one workspace keeps each kind of data.

    Order: a registered source/config/runtime mapping, then a configuration marker,
    then the project-local or legacy engine layout. Read-only.
    """
    root = Path(root).expanduser().resolve()
    found = find(root)
    if found:
        workspace_id, row = found
        config = Path(row["config_root"])
        if not config.is_dir():
            # Never fall back to an empty store in a disconnected folder (AC-12).
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                f"등록된 설정 위치에 접근할 수 없습니다: {config}. 저장 장치를 연결하거나 "
                "devroster settings location으로 위치를 복구해주세요.",
            )
        marker = _marker(config)
        if marker and marker.get("workspace_id") not in {None, workspace_id}:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "설정 위치의 workspace ID가 앱 매핑과 다릅니다. 다른 DB를 연결하지 않습니다.",
            )
        return external_locations(
            workspace_id,
            Path(row["source_root"]),
            config,
            Path(row["runtime_root"]),
            management_root(),
        )
    for config in (root, root / RUNTIME_DIR):
        marker = _marker(config)
        if marker is None:
            continue
        if "source_root" in marker:
            return _external_marker(config, marker)
        return local_locations(
            config.parent, config, studio_base=config / "studio", layout=PORTABLE
        )
    from agent_dev.core.paths import AGENT_DEV_ROOT, PROJECT_ROOT

    if root == Path(AGENT_DEV_ROOT).resolve():
        return local_locations(Path(PROJECT_ROOT).resolve(), root, layout=LEGACY)
    return local_locations(root, root)
