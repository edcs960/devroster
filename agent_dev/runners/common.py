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

    try:
        completed = subprocess.run(
            windows_command([command, "mcp", "list"]),
            cwd=PROJECT_ROOT,
            env=isolated_environment(),
            timeout=30,
            text=True, encoding="utf-8", errors="replace",
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{label} MCP server preflight did not finish within 30 seconds.") from None
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


def stream_communicate(process, input_text, timeout, on_line):
    """Like ``process.communicate`` but passes each output line to ``on_line(stream, line)``.

    Returns (stdout, stderr). On timeout the process is terminated (then killed) before
    TimeoutExpired is raised, so callers' cleanup only has to wait for it.
    """
    import contextlib
    import threading

    chunks = {"stdout": [], "stderr": []}

    def pump(name, pipe):
        for line in iter(pipe.readline, ""):
            chunks[name].append(line)
            # A live-log failure must never break the run.
            with contextlib.suppress(Exception):
                on_line(name, line)

    readers = [
        threading.Thread(target=pump, args=(name, pipe), daemon=True)
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))
        if pipe is not None
    ]
    for reader in readers:
        reader.start()
    if process.stdin is not None:
        try:
            if input_text:
                process.stdin.write(input_text)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for reader in readers:
            reader.join(timeout=5)
        raise subprocess.TimeoutExpired(process.args, timeout) from None
    # A grandchild that keeps the pipe open must not hang the run after the CLI exited.
    for reader in readers:
        reader.join(timeout=30)
    return "".join(chunks["stdout"]), "".join(chunks["stderr"])
