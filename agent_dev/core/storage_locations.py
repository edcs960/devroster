"""Storage-location types and management-root discovery.

Pure path composition only: nothing here creates folders or databases. Registry
I/O lives in ``adapters.storage_registry``; the decision which layout a folder uses
lives in ``adapters.workspace_locator``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from agent_dev.domain.errors import DomainError, ErrorCode

HOME_OVERRIDE = "DEVROSTER_HOME"
BOOTSTRAP_FILE = "bootstrap.json"
BOOTSTRAP_VERSION = 1
RUNTIME_DIR = ".agent-dev"

# Registered workspace IDs are uuid4 hex; they become credential folder names.
WORKSPACE_ID = re.compile(r"[0-9a-f]{32}")

# Layout names, recorded so callers never infer the layout from a parent folder.
EXTERNAL = "external"
PORTABLE = "portable"
LEGACY = "legacy"
LOCAL = "local"


@dataclass(frozen=True)
class WorkspaceLocations:
    workspace_id: str
    source_root: Path
    config_root: Path
    state_db: Path
    studio_db: Path
    role_dir: Path
    environment_file: Path
    oauth_dir: Path
    cache_root: Path
    runtime_root: Path
    worktree_root: Path
    output_root: Path
    layout: str = LOCAL

    @property
    def needs_trust(self) -> bool:
        """Only configuration that can travel with the project folder needs a decision."""
        # The legacy engine folder and a registered mapping are folders the user chose.
        registered = self.layout == EXTERNAL and bool(self.workspace_id)
        return self.layout != LEGACY and not registered


def location_error(message: str) -> DomainError:
    return DomainError(ErrorCode.INVALID_INPUT, message)


def default_app_home(environ=None) -> Path:
    """The OS application folder, ignoring any override or bootstrap pointer."""
    environ = os.environ if environ is None else environ
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DevRoster"
    if os.name == "nt":
        return Path(environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "DevRoster"
    return Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "devroster"


def bootstrap_file(environ=None) -> Path:
    return default_app_home(environ) / BOOTSTRAP_FILE


def checked_root(path: Path, label: str) -> Path:
    """An absolute folder path; a missing folder is allowed, a file or relative path is not."""
    if not path.is_absolute():
        raise location_error(f"{label}는 절대 경로여야 합니다: {path}")
    if path.exists() and not path.is_dir():
        raise location_error(f"{label}가 폴더가 아닙니다: {path}")
    return path


def read_bootstrap(environ=None) -> dict | None:
    path = bootstrap_file(environ)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise location_error(
            f"관리 폴더 위치 파일을 읽지 못했습니다. 관리 폴더를 다시 선택해주세요: {path}"
        ) from None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != BOOTSTRAP_VERSION
        or not isinstance(value.get("management_root"), str)
        or not value["management_root"]
    ):
        raise location_error(
            f"관리 폴더 위치 파일 형식을 확인해주세요. 관리 폴더를 다시 선택해주세요: {path}"
        )
    return value


def management_root(environ=None) -> Path:
    """Explicit DEVROSTER_HOME, then the bootstrap pointer, then the OS default.

    A pointer to a missing or unreadable folder is an error, never a silent fallback
    to another location or an empty store.
    """
    environ = os.environ if environ is None else environ
    configured = environ.get(HOME_OVERRIDE)
    if configured:
        return checked_root(Path(configured).expanduser(), HOME_OVERRIDE)
    pointer = read_bootstrap(environ)
    if pointer is not None:
        root = checked_root(Path(pointer["management_root"]), "관리 폴더")
        if not root.is_dir():
            raise location_error(
                f"관리 폴더에 접근할 수 없습니다. 연결을 확인하거나 다시 선택해주세요: {root}"
            )
        return root
    return default_app_home(environ)


def _runtime_paths(source: Path, runtime: Path) -> dict:
    return {
        "runtime_root": runtime,
        "worktree_root": runtime / "worktrees",
        "output_root": source / "output",
    }


def local_locations(
    source: Path,
    config: Path,
    *,
    studio_base: Path | None = None,
    layout: str = LOCAL,
    workspace_id: str = "",
) -> WorkspaceLocations:
    """Project-local and legacy layouts: every DevRoster file below ``source/.agent-dev``.

    The Studio DB keeps its existing location: ``<config>/studio`` for a workspace marker,
    ``<config>/.agent-dev/studio`` otherwise (legacy engine folder).
    """
    managed = source / RUNTIME_DIR
    studio = studio_base if studio_base is not None else config / RUNTIME_DIR / "studio"
    return WorkspaceLocations(
        workspace_id=workspace_id,
        source_root=source,
        config_root=config,
        state_db=managed / "state.db",
        studio_db=studio / "studio.db",
        role_dir=managed / "roles",
        environment_file=managed / "environment.json",
        oauth_dir=managed / "oauth",
        cache_root=managed,
        layout=layout,
        **_runtime_paths(source, managed),
    )


def valid_workspace_id(workspace_id) -> str:
    if not isinstance(workspace_id, str) or not WORKSPACE_ID.fullmatch(workspace_id):
        raise location_error("workspace ID 형식이 올바르지 않습니다.")
    return workspace_id


def external_locations(
    workspace_id: str, source: Path, config: Path, runtime: Path, management: Path
) -> WorkspaceLocations:
    """Settings and databases in a managed folder; worktrees and output stay with the source.

    Only a registered mapping reaches this: its ID selects the private credential folder.
    """
    base = management / "credentials" / "projects"
    credentials = base / valid_workspace_id(workspace_id)
    if not credentials.resolve().is_relative_to(base.resolve()):
        raise location_error("인증 저장 위치가 관리 폴더 밖을 가리킵니다.")
    return WorkspaceLocations(
        workspace_id=workspace_id,
        source_root=source,
        config_root=config,
        state_db=config / "state.db",
        studio_db=config / "studio" / "studio.db",
        role_dir=config / "roles",
        environment_file=credentials / "environment.json",
        oauth_dir=credentials / "oauth",
        cache_root=config / "cache",
        layout=EXTERNAL,
        **_runtime_paths(source, runtime),
    )


def unregistered_external_locations(
    source: Path, config: Path, runtime: Path
) -> WorkspaceLocations:
    """An external marker without a registry entry: no workspace ID, no shared credential
    folder. Private values stay inside its own configuration folder."""
    return WorkspaceLocations(
        workspace_id="",
        source_root=source,
        config_root=config,
        state_db=config / "state.db",
        studio_db=config / "studio" / "studio.db",
        role_dir=config / "roles",
        environment_file=config / "environment.json",
        oauth_dir=config / "oauth",
        cache_root=config / "cache",
        layout=EXTERNAL,
        **_runtime_paths(source, runtime),
    )
