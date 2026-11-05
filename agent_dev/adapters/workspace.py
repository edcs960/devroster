"""Project-local configuration independent of the package installation directory."""

import json
import os
import shutil
from importlib.resources import files
from pathlib import Path

from agent_dev.domain.errors import DomainError, ErrorCode

MARKER = "workspace.json"


def engine_configuration(config: Path) -> Path:
    """The engine folder when `config` is the project folder of a legacy engine layout.

    A legacy layout keeps settings and v2 flags under the engine folder while the project
    folder holds the canonical DB, so commands given either folder must resolve the same
    settings.
    """
    from agent_dev.adapters.workflow_runtime import project_root
    from agent_dev.core import paths

    config = Path(config)
    engine = Path(paths.AGENT_DEV_ROOT).resolve()
    if (
        config != engine
        and not (config / ".agent-dev" / MARKER).exists()
        and (engine / "config/runtime.json").is_file()
        and project_root(engine) == config
    ):
        return engine
    return config


def configuration_root(root: Path) -> Path:
    from agent_dev.adapters.storage_registry import registered_config

    root = Path(root).expanduser().resolve()
    # A registered workspace mapping outranks a project-local marker left in the source.
    registered = registered_config(root)
    if registered is not None:
        return registered
    if (root / ".agent-dev" / MARKER).is_file():
        return root / ".agent-dev"
    return root


def discover(start: Path | None = None) -> Path | None:
    from agent_dev.adapters.storage_registry import registered_config

    start = Path(start or Path.cwd()).resolve()
    for candidate in (start, *start.parents):
        registered = registered_config(candidate)
        if registered is not None:
            return registered
        if (candidate / ".agent-dev" / MARKER).is_file():
            return candidate / ".agent-dev"
    return None


def default_root() -> Path:
    configured = os.environ.get("AGENT_DEV_CONFIG_ROOT")
    if configured:
        return configuration_root(Path(configured))
    found = discover()
    if found:
        return found
    from agent_dev.core.paths import AGENT_DEV_ROOT

    return AGENT_DEV_ROOT


def portable_root() -> Path | None:
    root = configuration_root(default_root())
    return root if (root / MARKER).is_file() else None


def metadata(root: Path) -> dict | None:
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


def initialize(
    root: Path,
    *,
    name: str = "",
    project_id: str = "",
    runner="codex_cli",
    paths=(),
    preset="generic",
):
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository
    from agent_dev.core.output_paths import slugify

    root = Path(root).expanduser().resolve()
    managed = root / ".agent-dev"
    name = name.strip() or root.name
    project_id = project_id.strip() or slugify(name)
    if (
        not name
        or not project_id
        or Path(project_id).name != project_id
        or project_id in {".", ".."}
        or "\\" in project_id
        or runner not in {"codex_cli", "claude_cli", "gemini_cli"}
    ):
        raise DomainError(ErrorCode.INVALID_INPUT, "Invalid project identifier or runner.")
    from agent_dev.adapters.presets import profile as preset_profile
    from agent_dev.adapters.presets import select

    preset = select(preset, root)
    from agent_dev.adapters.workflow_definitions import new_project_profile
    initial_profile = new_project_profile(preset_profile(preset))
    allowed = list(paths or initial_profile["allowed_paths"])
    if any(
        not isinstance(path, str)
        or not path
        or Path(path).is_absolute()
        or ".." in Path(path).parts
        or "\\" in path
        or path in {".", ".git", ".agent-dev"}
        or path.startswith((".git/", ".agent-dev/"))
        for path in allowed
    ):
        raise DomainError(ErrorCode.INVALID_INPUT, "Use explicit relative source/test allow-paths.")
    root.mkdir(parents=True, exist_ok=True)
    try:
        managed.mkdir(mode=0o700)
    except FileExistsError:
        raise DomainError(
            ErrorCode.POLICY_VIOLATION, "Workspace already exists; init never overwrites it."
        ) from None
    try:
        template = files("agent_dev.templates")
        profile = initial_profile
        profile["repositories"]["source"]["allowed_paths"] = allowed
        profile["allowed_paths"] = allowed
        profile["runners"][0]["provider"] = runner
        profile["runners"][0]["id"] = runner.removesuffix("_cli")
        values = {
            MARKER: {
                "format_version": 1,
                "project_id": project_id,
                "name": name,
                "engine_version": "0.3.0",
                "preset": preset,
            },
            "config/runtime.json": {
                "system_name": name,
                # New workspaces have no legacy data to migrate (ADR 0003).
                "orchestrator_v2": {
                    "read": True,
                    "dual_write": True,
                    "execute": True,
                    "publish": True,
                    "legacy_readonly": False,
                },
            },
            "config/profile.json": profile,
            "starter/tasks.json": [{"id": "TASK-001", "title": "First task", "system_name": name}],
        }
        for relative, value in values.items():
            path = managed / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            path.chmod(0o600)
        for source, relative in (
            ("design.md", "starter/design.md"),
            ("QUICKSTART.md", "README.md"),
        ):
            (managed / relative).write_text(
                template.joinpath(source).read_text(encoding="utf-8"), encoding="utf-8"
            )
        WorkflowRepository(Database(managed / "state.db")).register_project(
            project_id, name, profile
        )
    except BaseException:
        shutil.rmtree(managed)
        raise
    return {
        "project_id": project_id,
        "name": name,
        "configuration_root": str(managed),
        "database": str(managed / "state.db"),
        "guide": str(managed / "README.md"),
        "preset": preset,
        "operator_actions": [
            "Run devroster doctor; review config/profile.json allow-paths, test gates and runner authentication.",
            "Add /.agent-dev/ and generated output directories to your repository ignore rules.",
        ],
    }
