"""Shared runner primitives: protocol, errors, env loading, MCP preflight."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from agent_dev.adapters.windows_launch import windows_command
from agent_dev.core.models import AgentConfig, PipelineStep, Task
from agent_dev.core.paths import AGENT_DEV_ROOT, PROJECT_ROOT


class AgentRunner(Protocol):
    def run(self, task: Task, agent: AgentConfig, step: PipelineStep, prompt: str) -> str:
        ...


class CodexUsageLimitError(RuntimeError):
    """Raised when Codex CLI reports a quota or usage-limit condition."""


class GeminiUsageLimitError(RuntimeError):
    """Gemini CLI usage capacity exhausted."""


class ClaudeUsageLimitError(RuntimeError):
    """Raised when Claude CLI reports a usage-limit condition."""


class RunnerFailoverExhaustedError(RuntimeError):
    """Raised when every configured CLI runner has hit a usage limit."""


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_cli_env_files() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    load_env_file(AGENT_DEV_ROOT / ".env")


def resolve_project_path(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def is_usage_limit_message(message: str) -> bool:
    normalized = message.lower()
    indicators = [
        "usage limit",
        "rate limit",
        "quota",
        "insufficient_quota",
        "limit reached",
        "limit exceeded",
        "weekly limit",
        "5 hour",
        "5-hour",
        "usage cap",
        "message limit",
        "session limit",
        "maximum usage",
        "try again later",
        "too many requests",
        "you've hit your",
        "you have hit your",
        "resets",
    ]
    return any(indicator in normalized for indicator in indicators)


def ensure_mcp_servers(
    *,
    command: str,
    required_mcp_servers: dict,
    step: PipelineStep,
    label: str,
    line_indicates_enabled: Callable[[str, str], bool],
    fix_hint: str,
) -> None:
    """Shared `<cli> mcp list` preflight for phase-required MCP servers.

    ``line_indicates_enabled(name, line)`` encodes each CLI's list format
    (Codex: first token + 'enabled'; Claude: name + Connected/✓/enabled).
    """
    required = [
        name
        for name, phase_ids in required_mcp_servers.items()
        if not phase_ids or step.step_id in set(phase_ids)
    ]
    if not required:
        return

    from agent_dev.core.cli_home import isolated_environment

    completed = subprocess.run(
        windows_command([command, "mcp", "list"]),
        cwd=PROJECT_ROOT,
        env=isolated_environment(),
        text=True, encoding="utf-8", errors="replace",
        capture_output=True,
        check=False,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0:
        raise RuntimeError(
            f"{label} MCP server preflight failed.\n"
            f"Command: {command} mcp list\n"
            f"Return Code: {completed.returncode}\n"
            f"Output:\n{output.strip() or '(empty)'}"
        )

    missing = [
        name
        for name in required
        if not any(line_indicates_enabled(name, line) for line in output.splitlines())
    ]
    if missing:
        raise RuntimeError(
            f"Required {label} MCP server is not enabled for this phase.\n"
            f"Phase: {step.step_id}\n"
            f"Missing: {', '.join(missing)}\n"
            f"{fix_hint}"
        )
