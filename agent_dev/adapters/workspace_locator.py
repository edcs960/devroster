"""The one place that decides where a workspace keeps its data.

Given any folder a caller holds (the source folder, a configuration folder, the legacy
engine folder), the functions here answer, in this fixed order:

1. a registered mapping in the management folder (``storage_registry``),
2. a ``workspace.json`` marker in the folder or its ``.agent-dev``,
3. the legacy engine layout (settings under the engine folder, DB with the project),
4. a plain project folder that has no DevRoster data yet.

``locate`` returns the whole answer as one ``Workspace``. The older single-purpose names
(``configuration_root``, ``workspace_locations``, ``project_root`` ...) are kept because
many callers need only one part; they are defined here and re-exported by the modules
that used to own them. Everything here is read-only: nothing creates a folder, a
database or a registry entry.

This module imports the registry and path composition only. It must not import
``workspace``, ``workflow_runtime``, ``project_setup`` or ``workspace_registry``; those
import this module (see ``tests/test_architecture.py``).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from agent_dev.adapters.storage_registry import find, read, registered_config
from agent_dev.core import paths
from agent_dev.core.storage_locations import (
    HOME_OVERRIDE,
    LEGACY,
    PORTABLE,
    RUNTIME_DIR,
    WorkspaceLocations,
    external_locations,
    local_locations,
    location_error,
    management_root,
    read_bootstrap,
    unregistered_external_locations,
)
from agent_dev.domain.errors import DomainError, ErrorCode

MARKER = "workspace.json"


@dataclass(frozen=True)
class Workspace:
    """Where one workspace lives, decided once."""

    config_root: Path  # settings folder callers pass around as "root"
    source_root: Path  # the project's source folder
    locations: WorkspaceLocations
    marker: dict | None  # validated workspace.json content, when the folder has one

    @property
    def layout(self) -> str:
        return self.locations.layout

    @property
    def state_db(self) -> Path:
        return self.locations.state_db

    @property
    def managed_root(self) -> Path:
        """The folder that holds the canonical DB: ``<source>/.agent-dev`` or the external config."""
        return self.locations.state_db.parent

    @property
    def legacy(self) -> bool:
        """Settings live in the engine folder, apart from the project and its DB folder."""
        return self.config_root != self.source_root and self.config_root != self.managed_root

    @property
    def needs_trust(self) -> bool:
        return self.locations.needs_trust


def configuration_root(root: Path) -> Path:
    """The configuration folder for a source, runtime or configuration folder."""
    root = Path(root).expanduser().resolve()
    # A registered workspace mapping outranks a project-local marker left in the source.
    registered = registered_config(root)
    if registered is not None:
        return registered
    if (root / RUNTIME_DIR / MARKER).is_file():
        return root / RUNTIME_DIR
    return root


def discover(start: Path | None = None) -> Path | None:
    """The configuration folder of the workspace that contains ``start``, if any."""
    start = Path(start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        registered = registered_config(candidate)
        if registered is not None:
            return registered
        if (candidate / RUNTIME_DIR / MARKER).is_file():
            return candidate / RUNTIME_DIR
    return None


def default_root() -> Path:
    configured = os.environ.get("AGENT_DEV_CONFIG_ROOT")
    if configured:
        return configuration_root(Path(configured))
    return discover() or paths.AGENT_DEV_ROOT


def portable_root() -> Path | None:
    root = configuration_root(default_root())
    return root if (root / MARKER).is_file() else None


def metadata(root: Path) -> dict | None:
    """The validated project marker of a workspace, or None when it has no marker."""
    path = configuration_root(root) / MARKER
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("format_version") != 1
        or not isinstance(value.get("project_id"), str)
        or not value["project_id"]
        or not isinstance(value.get("name"), str)
    ):
        raise DomainError(ErrorCode.INVALID_INPUT, "Unsupported workspace marker.")
    return value


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
    if root == Path(paths.AGENT_DEV_ROOT).resolve():
        return local_locations(Path(paths.PROJECT_ROOT).resolve(), root, layout=LEGACY)
    return local_locations(root, root)


def project_root(root: Path) -> Path:
    """The source folder of the workspace selected by a source or configuration folder."""
    config = configuration_root(root)
    metadata(config)  # Reject an unsupported marker before resolving.
    return workspace_locations(config).source_root


def engine_configuration(config: Path) -> Path:
    """The engine folder when ``config`` is the project folder of a legacy engine layout.

    A legacy layout keeps settings and v2 flags under the engine folder while the project
    folder holds the canonical DB, so commands given either folder must resolve the same
    settings.
    """
    config = Path(config)
    engine = Path(paths.AGENT_DEV_ROOT).resolve()
    if (
        config != engine
        and not (config / RUNTIME_DIR / MARKER).exists()
        and (engine / "config/runtime.json").is_file()
        and project_root(engine) == config
    ):
        return engine
    return config


def settings_root(root: Path) -> Path:
    """The folder whose settings apply to ``root``: its configuration folder, or the engine
    folder when ``root`` is the project folder of a legacy layout."""
    return engine_configuration(configuration_root(Path(root)))


def locate(root: Path) -> Workspace:
    """Everything about the workspace selected by ``root``, decided in one pass."""
    config = settings_root(root)
    return Workspace(
        config_root=config,
        source_root=project_root(config),
        locations=workspace_locations(config),
        marker=metadata(config),
    )


def state_db_path(root: Path) -> Path:
    """The canonical DB of the workspace selected by ``root`` (source or config folder)."""
    return workspace_locations(configuration_root(Path(root))).state_db


def remembered_configuration(root: Path, remembered, *, checked) -> Path:
    """The configuration folder for a project from the app's project list.

    ``remembered()`` returns what the list keeps: ``configurations`` (source folder to
    configuration folder) and ``previous`` (the last opened folder). It is only called
    when no mapping is registered. ``checked`` turns a remembered value into a path or
    raises. Order: a registered mapping, the listed configuration folder, the last
    opened folder when it belongs to this project, then the folder itself.
    """
    root = Path(root)
    registered = registered_config(root)
    if registered is not None:
        return registered
    configurations, previous = remembered()
    if isinstance(configurations, dict) and str(root) in configurations:
        return checked(configurations[str(root)])
    if isinstance(previous, str) and str(locate(checked(previous)).source_root) == str(root):
        return checked(previous)
    return root


def location_report(root: Path) -> dict:
    """Where the management root and the selected workspace keep their data (no secrets)."""
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
