import json
import os
from pathlib import Path


def resolve_agent_dev_root() -> Path:
    configured = os.environ.get("AGENT_DEV_CONFIG_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        if (candidate / ".agent-dev/workspace.json").is_file():
            return candidate / ".agent-dev"
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").exists() and (candidate / "agent_dev").exists():
            return candidate
    return Path.cwd().resolve()


AGENT_DEV_ROOT = resolve_agent_dev_root()


def resolve_project_root() -> Path:
    configured = os.environ.get("AGENT_DEV_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()

    marker = AGENT_DEV_ROOT / "workspace.json"
    if marker.is_file():
        # An external configuration folder names its source folder explicitly.
        try:
            source = json.loads(marker.read_text(encoding="utf-8")).get("source_root")
        except (OSError, ValueError, AttributeError):
            source = None
        if isinstance(source, str) and Path(source).is_absolute():
            return Path(source).resolve()
        return AGENT_DEV_ROOT.parent

    for candidate in [AGENT_DEV_ROOT, *AGENT_DEV_ROOT.parents]:
        if (candidate / "AGENTS.md").exists():
            return candidate

    if (AGENT_DEV_ROOT / "pyproject.toml").is_file():
        return AGENT_DEV_ROOT.parent
    return AGENT_DEV_ROOT


PROJECT_ROOT = resolve_project_root()
CONFIG_DIR = AGENT_DEV_ROOT / "config"
DATA_DIR = AGENT_DEV_ROOT / "data"
