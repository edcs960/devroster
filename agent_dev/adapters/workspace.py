"""Creating a project-local workspace (``devroster init``).

Where an existing workspace lives is decided in ``workspace_locator``; the names below are
re-exported for the callers that import them from here.
"""

import json
import shutil
from importlib.resources import files
from pathlib import Path

from agent_dev.adapters.workspace_locator import (
    MARKER,
    configuration_root,
    default_root,
    discover,
    engine_configuration,
    metadata,
    portable_root,
)
from agent_dev.domain.errors import DomainError, ErrorCode

__all__ = [
    "MARKER",
    "configuration_root",
    "default_root",
    "discover",
    "engine_configuration",
    "initialize",
    "metadata",
    "portable_root",
]


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
