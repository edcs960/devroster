"""Claude CLI runner."""

from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path

from agent_dev.adapters.windows_launch import windows_command
from agent_dev.core.cli_home import RUNNER_NOTE, claude_settings_file, isolated_environment
from agent_dev.core.models import AgentConfig, PipelineStep, Task
from agent_dev.core.paths import PROJECT_ROOT
from agent_dev.domain.model_routing import select_model
from agent_dev.runners.common import (
    ClaudeUsageLimitError,
    ensure_mcp_servers,
    is_usage_limit_message,
    load_cli_env_files,
    resolve_project_path,
)
from agent_dev.storage.process_registry import ProcessRegistry


class ClaudeResumeUnavailableError(RuntimeError):
    """The requested conversation no longer exists; callers retry with a new session."""


class ClaudeCliRunner:
    """Runs Claude CLI against generated run prompt files."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        if config.get("load_env_files", True):
            load_cli_env_files()
        self.process_registry_path = config.get("process_registry_path")
        self.lease_config = config.get("lease_config")
        self.process_env = config.get("process_env")
        self.workspace_path = Path(config.get("workspace_path", PROJECT_ROOT)).resolve()
        self.timeout_seconds = int(config.get("timeout_seconds", 1800))
        self.inline_instruction = config.get("inline_instruction", "Return one Harness Control JSON result. Follow the approved phase scope.\n")
        self.collect_usage = bool(config.get("collect_usage", False))
        self.last_usage = {}
        # Set per invocation by the caller; last_session_id is read back after run().
        self.resume_session_id = ""
        self.last_session_id = ""
        self.command = config.get("command", "claude")
        self.model = config.get("model", "")
        self.model_config = deepcopy({key: config[key] for key in ("model", "effort", "routing") if key in config})
        self.last_selection = {}
        self.permission_mode = config.get("permission_mode", "dontAsk")
        self.output_format = config.get("output_format", "text")
        self.extra_args = list(config.get("extra_args", []))
        self.add_dirs = [resolve_project_path(path) for path in config.get("add_dirs", [])]
        self.mcp_config = [
            str(resolve_project_path(path))
            for path in config.get("mcp_config", [])
        ]
        self.inject_shared_mcp = bool(config.get("inject_shared_mcp", True))
        self.required_mcp_servers = dict(config.get("required_mcp_servers", {}))

    def isolation_args(self) -> list[str]:
        """DevRoster MCP servers, app-private settings, no project instruction files."""
        args = []
        shared = PROJECT_ROOT / ".mcp.json"
        if self.inject_shared_mcp and shared.is_file() and str(shared) not in self.mcp_config:
            args.extend(["--mcp-config", str(shared)])
        args.append("--strict-mcp-config")
        if self.supports_option("--setting-sources"):
            args.extend(["--setting-sources", "user"])
        # claudeMdExcludes keeps project CLAUDE.md/AGENTS.md/rules out even with OAuth
        # login (--bare would need an API key); the internal note replaces them.
        args.extend(
            [
                "--settings",
                str(claude_settings_file(self.workspace_path, PROJECT_ROOT, *self.add_dirs)),
                "--append-system-prompt",
                RUNNER_NOTE,
            ]
        )
        return args

    def supports_option(self, option: str) -> bool:
        if getattr(self, "_help_text", None) is None:
            try:
                completed = subprocess.run(
                    windows_command([self.command, "--help"]),
                    env=isolated_environment(self.process_env),
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=15,
                    check=False,
                )
                self._help_text = completed.stdout + completed.stderr
            except Exception:  # noqa: BLE001 - best effort; skip optional flags when unknown
                self._help_text = ""
        return option in self._help_text

    def run(self, task: Task, agent: AgentConfig, step: PipelineStep, prompt: str) -> str:
        self.last_usage = {}
        self.last_session_id = ""
        self.last_selection = select_model(self.model_config, task.to_dict(), step.step_id)
        self.ensure_required_mcp_servers(step)
        instruction = self.inline_instruction + prompt

        command = [
            self.command,
            "--print",
            "--output-format",
            self.output_format,
            *(["--verbose"] if self.output_format == "stream-json" else []),
            "--permission-mode",
            self.permission_mode,
        ]
        if self.last_selection["model"]:
            command.extend(["--model", self.last_selection["model"]])
        if self.last_selection["effort"]:
            command.extend(["--effort", self.last_selection["effort"]])
        if self.resume_session_id:
            command.extend(["--resume", self.resume_session_id])
        for path in self.add_dirs:
            command.extend(["--add-dir", str(path)])
        for path in self.mcp_config:
            command.extend(["--mcp-config", path])
        command.extend(self.isolation_args())
        command.extend(self.extra_args)

        if self.lease_config:
            import os
            import sys

            from agent_dev.adapters import process_guard
            grant = self.lease_config
            command = [sys.executable, process_guard.__file__, "--parent", str(os.getpid()),
                       "--db", grant["db"], "--resource", grant["resource"],
                       "--holder", grant["holder"], "--token", str(grant["token"]), "--", *command]

        process = subprocess.Popen(
            windows_command(command),
            env=isolated_environment(self.process_env),
            cwd=self.workspace_path,
            text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        registry = ProcessRegistry(Path(self.process_registry_path)) if self.process_registry_path else ProcessRegistry()
        registry.register(
            task_id=task.task_id,
            phase=step.step_id,
            pid=process.pid,
            command=command,
            kind="claude_cli",
        )
        try:
            live = getattr(self, "live_log", None)
            if live is not None:
                from agent_dev.runners.common import stream_communicate

                stdout, stderr = stream_communicate(process, instruction, self.timeout_seconds, live.write)
            else:
                stdout, stderr = process.communicate(input=instruction, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            # Give the lease supervisor a chance to terminate the provider tree.
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise
        finally:
            registry.unregister(process.pid)

        if process.returncode != 0:
            message = "\n".join(
                [
                    "Claude CLI execution failed.",
                    f"Command: {' '.join(command[:2])} ...",
                    f"Return Code: {process.returncode}",
                    "stdout:",
                    stdout.strip() or "(empty)",
                    "stderr:",
                    stderr.strip() or "(empty)",
                ]
            )
            if is_usage_limit_message(message):
                raise ClaudeUsageLimitError(message)
            if self.resume_session_id and "No conversation found" in message:
                raise ClaudeResumeUnavailableError(message)
            raise RuntimeError(message)
        if self.collect_usage:
            from agent_dev.runners.usage import claude_result, claude_session

            result, self.last_usage = claude_result(stdout)
            self.last_session_id = claude_session(stdout)
            return result
        return stdout.strip()

    def ensure_required_mcp_servers(self, step: PipelineStep) -> None:
        ensure_mcp_servers(
            command=self.command,
            required_mcp_servers=self.required_mcp_servers,
            step=step,
            label="Claude",
            line_indicates_enabled=lambda name, line: name in line
            and ("Connected" in line or "✓" in line or "enabled" in line),
            fix_hint=(
                "Run `claude mcp list` and approve/configure the missing server "
                "before rerunning the pipeline."
            ),
        )
