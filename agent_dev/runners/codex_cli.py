"""Codex CLI runner and its MCP config injection helpers."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path

from agent_dev.core.cli_home import isolated_environment
from agent_dev.core.models import AgentConfig, PipelineStep, Task
from agent_dev.core.paths import PROJECT_ROOT
from agent_dev.domain.model_routing import select_model
from agent_dev.runners.common import (
    CodexUsageLimitError,
    ensure_mcp_servers,
    is_usage_limit_message,
    load_cli_env_files,
    read_optional_text,
    resolve_project_path,
)
from agent_dev.storage.process_registry import ProcessRegistry


class CodexResumeUnavailableError(RuntimeError):
    """The requested thread no longer exists; callers retry with a new session."""


class CodexCliRunner:
    """Runs Codex CLI against generated run prompt files."""

    def __init__(self, config: dict | None = None) -> None:
        config = config or {}
        if config.get("load_env_files", True):
            load_cli_env_files()
        self.process_registry_path = config.get("process_registry_path")
        self.process_env = config.get("process_env")
        self.workspace_path = Path(config.get("workspace_path", PROJECT_ROOT)).resolve()
        self.timeout_seconds = int(config.get("timeout_seconds", 1800))
        self.stdin_prompt = bool(config.get("stdin_prompt", False))
        self.inline_instruction = config.get("inline_instruction", "Return one Harness Control JSON result. Follow the approved phase scope.\n")
        self.collect_usage = bool(config.get("collect_usage", False))
        self.last_usage = {}
        # Set per invocation by the caller; last_session_id is read back after run().
        self.resume_session_id = ""
        self.last_session_id = ""
        self.command = config.get("command", "codex")
        self.model = config.get("model", "")
        self.model_config = deepcopy({key: config[key] for key in ("model", "effort", "routing") if key in config})
        self.last_selection = {}
        self.sandbox = config.get("sandbox", "workspace-write")
        self.approval = config.get("approval", "never")
        self.skip_git_repo_check = bool(config.get("skip_git_repo_check", True))
        self.extra_args = list(config.get("extra_args", []))
        self.add_dirs = [resolve_project_path(path) for path in config.get("add_dirs", [])]
        self.required_mcp_servers = dict(config.get("required_mcp_servers", {}))
        # Inject the shared .mcp.json servers (the same file Claude CLI reads via
        # --mcp-config) into `codex exec` as `-c mcp_servers.*` config overrides so
        # both runners see the same MCP servers. Disable with codex_cli.inject_shared_mcp=false.
        self.inject_shared_mcp = bool(config.get("inject_shared_mcp", True))
        self._exec_help: str | None = None
        self._known_mcp_names: set[str] | None = None

    def run(self, task: Task, agent: AgentConfig, step: PipelineStep, prompt: str) -> str:
        self.last_usage = {}
        self.last_session_id = ""
        self.last_selection = select_model(self.model_config, task.to_dict(), step.step_id)
        self.ensure_required_mcp_servers(step)

        instruction = self.inline_instruction + prompt

        with tempfile.NamedTemporaryFile(
            mode="w+",
            encoding="utf-8",
            suffix=f"-{step.step_id}-codex-output.md",
            delete=False,
        ) as output_file:
            codex_output_path = Path(output_file.name)

        if self.resume_session_id:
            # `exec resume` has no -C, --sandbox or --add-dir: the process cwd selects the
            # workspace and config overrides carry the same sandbox.
            command = [
                self.command,
                "exec",
                "resume",
                self.resume_session_id,
                "-c",
                "sandbox_mode=" + _toml_scalar(self.sandbox),
                "--output-last-message",
                str(codex_output_path),
            ]
            if self.add_dirs:
                roots = ", ".join(_toml_scalar(str(path)) for path in self.add_dirs)
                command.extend(["-c", f"sandbox_workspace_write.writable_roots=[{roots}]"])
        else:
            command = [
                self.command,
                "exec",
                "-C",
                str(self.workspace_path),
                "--sandbox",
                self.sandbox,
                "--output-last-message",
                str(codex_output_path),
            ]
            for path in self.add_dirs:
                command.extend(["--add-dir", str(path)])
        if self.approval and self.supports_exec_option("--ask-for-approval"):
            command.extend(["--ask-for-approval", self.approval])
        if self.last_selection["model"]:
            command.extend(["--model", self.last_selection["model"]])
        if self.last_selection["effort"]:
            command.extend(["-c", "model_reasoning_effort=" + self.last_selection["effort"]])
        if self.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        # Rules arrive in the prompt; never merge the project's own AGENTS.md.
        command.extend(["-c", "project_doc_max_bytes=0"])
        if self.inject_shared_mcp:
            injectable = self._injectable_mcp_servers(read_shared_mcp_servers())
            command.extend(build_codex_mcp_config_args(injectable, self.process_env or os.environ))
        if self.collect_usage:
            command.append("--json")
        command.extend(self.extra_args)
        command.append("-" if self.stdin_prompt else instruction)

        if getattr(self, "lease_config", None):
            import sys

            from agent_dev.adapters import process_guard
            grant = self.lease_config
            command = [sys.executable, process_guard.__file__, "--parent", str(os.getpid()),
                       "--db", grant["db"], "--resource", grant["resource"],
                       "--holder", grant["holder"], "--token", str(grant["token"]), "--", *command]

        process = subprocess.Popen(
            command,
            env=isolated_environment(self.process_env),
            cwd=self.workspace_path,
            text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **({"stdin": subprocess.PIPE} if self.stdin_prompt else {}),
        )
        registry = ProcessRegistry(Path(self.process_registry_path)) if self.process_registry_path else ProcessRegistry()
        registry.register(
            task_id=task.task_id,
            phase=step.step_id,
            pid=process.pid,
            command=command,
            kind="codex_cli",
        )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds, **({"input": instruction} if self.stdin_prompt else {}))
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

        output_text = read_optional_text(codex_output_path).strip()
        codex_output_path.unlink(missing_ok=True)
        if self.collect_usage:
            from agent_dev.runners.usage import codex_session, codex_usage

            self.last_usage = codex_usage(stdout)
            self.last_session_id = codex_session(stdout) if process.returncode == 0 else ""
        if process.returncode != 0:
            message = "\n".join(
                [
                    "Codex CLI execution failed.",
                    f"Command: {' '.join(command[:2])} ...",
                    f"Return Code: {process.returncode}",
                    "stdout:",
                    stdout.strip() or "(empty)",
                    "stderr:",
                    stderr.strip() or "(empty)",
                    "last message:",
                    output_text or "(empty)",
                ]
            )
            if is_usage_limit_message(message):
                raise CodexUsageLimitError(message)
            if self.resume_session_id and "no rollout found" in message:
                raise CodexResumeUnavailableError(message)
            raise RuntimeError(message)
        return output_text or stdout.strip()

    def ensure_required_mcp_servers(self, step: PipelineStep) -> None:
        ensure_mcp_servers(
            command=self.command,
            required_mcp_servers=self.required_mcp_servers,
            step=step,
            label="Codex",
            line_indicates_enabled=lambda name, line: bool(
                line.split() and line.split()[0] == name and "enabled" in line
            ),
            fix_hint="Run `codex mcp list` and configure the missing server before rerunning the pipeline.",
        )

    def _injectable_mcp_servers(self, servers: dict) -> dict:
        """Only inject .mcp.json servers Codex does not already define natively.

        The user's ~/.codex/config.toml may already configure servers (e.g. github
        with a bearer token, figma via OAuth); overriding those with raw -c headers
        would degrade working auth. So injection is strictly additive.
        """
        if not isinstance(servers, dict) or not servers:
            return {}
        known = self._codex_known_server_names()
        return {name: spec for name, spec in servers.items() if name not in known}

    def _codex_known_server_names(self) -> set[str]:
        if self._known_mcp_names is not None:
            return self._known_mcp_names
        names: set[str] = set()
        try:
            completed = subprocess.run(
                [self.command, "mcp", "list"],
                cwd=self.workspace_path,
                env=isolated_environment(self.process_env),
                text=True, encoding="utf-8", errors="replace",
                capture_output=True,
                check=False,
            )
            for line in (completed.stdout or "").splitlines():
                tokens = line.split()
                if tokens and tokens[0] not in {"Name", "Url", "Command"}:
                    names.add(tokens[0])
        except Exception:  # noqa: BLE001 - best effort; fall back to injecting all
            names = set()
        self._known_mcp_names = names
        return names

    def supports_exec_option(self, option: str) -> bool:
        if self._exec_help is None:
            completed = subprocess.run(
                [self.command, "exec", "--help"],
                cwd=self.workspace_path,
                env=isolated_environment(self.process_env),
                text=True, encoding="utf-8", errors="replace",
                capture_output=True,
                check=False,
            )
            self._exec_help = f"{completed.stdout}\n{completed.stderr}"
        return option in self._exec_help


def read_shared_mcp_servers() -> dict:
    """Read the shared MCP servers map from .mcp.json (best effort)."""
    try:
        from agent_dev.config import settings as settings_service

        return settings_service.read_mcp_servers()
    except Exception:  # noqa: BLE001 - MCP injection is optional, never fatal
        return {}


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env_refs(value: str, env) -> str:
    """Replace ${VAR} references with values from env (unknown -> left as-is)."""
    return _ENV_REF_PATTERN.sub(lambda m: env.get(m.group(1), m.group(0)), value)


def _toml_scalar(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_codex_mcp_config_args(servers: dict, env) -> list[str]:
    """Translate .mcp.json server specs into `codex exec -c` config overrides.

    Claude reads .mcp.json directly; Codex uses TOML config keys under
    ``mcp_servers.<name>``. ``${VAR}`` references in http headers / stdio env are
    resolved from ``env`` here because Codex does not expand them itself.

    Supported specs:
      - http : ``{"type": "http", "url": ..., "headers": {...}}``
      - stdio: ``{"command": ..., "args": [...], "env": {...}}``
    """
    args: list[str] = []
    if not isinstance(servers, dict):
        return args
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        base = f"mcp_servers.{name}"
        url = str(spec.get("url", "")).strip()
        command = str(spec.get("command", "")).strip()
        if url:
            args += ["-c", f"{base}.url={_toml_scalar(url)}"]
            headers = spec.get("headers", {})
            if isinstance(headers, dict):
                for header_key, header_value in headers.items():
                    resolved = _resolve_env_refs(str(header_value), env)
                    args += ["-c", f"{base}.http_headers.{header_key}={_toml_scalar(resolved)}"]
        elif command:
            args += ["-c", f"{base}.command={_toml_scalar(command)}"]
            spec_args = spec.get("args", [])
            if isinstance(spec_args, list) and spec_args:
                rendered = ", ".join(_toml_scalar(str(item)) for item in spec_args)
                args += ["-c", f"{base}.args=[{rendered}]"]
            spec_env = spec.get("env", {})
            if isinstance(spec_env, dict) and spec_env:
                rendered = ", ".join(
                    f"{key} = {_toml_scalar(_resolve_env_refs(str(val), env))}"
                    for key, val in spec_env.items()
                )
                args += ["-c", f"{base}.env={{{rendered}}}"]
    return args
