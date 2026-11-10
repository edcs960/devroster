"""Bounded existing runner adapter, with explicit workspace and MCP configuration."""

import subprocess
from pathlib import Path

from agent_dev.core.models import AgentConfig, PipelineStep, Task
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.session_output import resumed_context
from agent_dev.runners.claude_cli import ClaudeCliRunner, ClaudeResumeUnavailableError
from agent_dev.runners.codex_cli import CodexCliRunner, CodexResumeUnavailableError
from agent_dev.runners.common import (
    ClaudeUsageLimitError,
    CodexUsageLimitError,
    GeminiUsageLimitError,
)
from agent_dev.runners.gemini_cli import GeminiCliRunner


def _live_log_for(provider, bindings, attempt_id):
    """The attempt's live log, or None when it cannot be written (the run goes on without)."""
    from agent_dev.adapters.live_log import LiveLog, log_path

    if not bindings or not attempt_id:
        return None
    try:
        from agent_dev.adapters.secrets import registered_secrets
        from agent_dev.core.paths import PROJECT_ROOT

        secrets = registered_secrets(PROJECT_ROOT)
    except Exception:  # noqa: BLE001 - masking falls back to the generic secret patterns
        secrets = ()
    try:
        return LiveLog(log_path(bindings, attempt_id), provider, secrets)
    except OSError:
        return None


class CliRunnerAdapter:
    def __init__(self, provider: str, config: dict, workspace: Path, lease_config=None):
        approved_dirs = [Path(path).resolve() for path in config.get("approved_add_dirs", [])]
        if any(not path.is_relative_to(workspace.resolve().parent) for path in approved_dirs):
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "Additional repositories must be approved task worktrees.",
            )
        # Approved skill folders are read-only references inside the verified skill cache.
        skill_dirs = [Path(path).resolve() for path in config.get("skill_dirs", [])]
        if skill_dirs:
            from agent_dev.adapters.skill_store import cache_root

            skill_root = cache_root().resolve()
            outside = any(not path.is_relative_to(skill_root) for path in skill_dirs)
        else:
            outside = False
        if outside:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Skill folders must come from the verified cache."
            )
        config = dict(
            config,
            workspace_path=str(workspace),
            timeout_seconds=1800,
            inject_shared_mcp=False,
            inline_prompt=True,
            add_dirs=[],
            extra_args=[],
            load_env_files=False,
            collect_usage=True,
            # Events while running (tool calls, messages) for the live log; the final result
            # is the stream's last "result" event (runners/usage.py).
            output_format="stream-json",
            process_registry_path=str(
                Path(lease_config["db"]).parent / "data/process_registry.json"
            )
            if lease_config
            else None,
        )
        # MCP is absent by default; only grants from approved snapshots may add it.
        config["mcp_config"] = []
        config["required_mcp_servers"] = {}
        factories = {
            "codex_cli": CodexCliRunner,
            "claude_cli": ClaudeCliRunner,
            "gemini_cli": GeminiCliRunner,
        }
        if provider not in factories:
            raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE, "Unknown runner adapter.")
        # Approved verification commands a Claude code role may run while working (check_server).
        self.project_checks = list(config.pop("project_checks", []) or [])
        self.provider, self.runner = provider, factories[provider](config)
        # Codex reads outside the workspace already and --add-dir would grant writes there.
        self.runner.add_dirs = approved_dirs + ([] if provider == "codex_cli" else skill_dirs)
        self.runner.lease_config = lease_config
        if provider == "claude_cli":
            self.runner.extra_args = [
                "--strict-mcp-config",
                "--setting-sources",
                "",
                "--tools",
                "Read,Grep,Glob",
            ]
        elif provider == "codex_cli":
            # Inspect the same app-private home that the runner actually launches with.
            import tomllib

            from agent_dev.core.cli_home import isolated_environment

            source = Path(isolated_environment(self.runner.process_env)["CODEX_HOME"]) / "config.toml"
            if source.is_file():
                names = tomllib.loads(source.read_text(encoding="utf-8")).get("mcp_servers", {}).keys()
                self.runner.extra_args = [
                    argument
                    for name in names
                    for argument in ["-c", "mcp_servers." + name + ".enabled=false"]
                ]

    CHECK_TOOL = "mcp__devroster__run_project_check"

    def _attach_checks(self, code_allowed):
        """Give a Claude code role the approved checks as an MCP tool for this call only."""
        if self.provider != "claude_cli" or not code_allowed or not self.project_checks:
            return None
        import json
        import sys
        import tempfile

        folder = Path(tempfile.mkdtemp(prefix="devroster-checks-"))
        config, servers = folder / "checks.json", folder / "mcp.json"
        config.write_text(json.dumps({"checks": self.project_checks, "max_calls": 10}), encoding="utf-8")
        servers.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "devroster": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "agent_dev.adapters.check_server", str(config)],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self._saved = (list(self.runner.mcp_config), list(self.runner.extra_args))
        self.runner.mcp_config = [*self.runner.mcp_config, str(servers)]
        self.runner.extra_args = [*self.runner.extra_args, "--allowedTools", self.CHECK_TOOL]
        return folder

    def _detach_checks(self, folder):
        if folder is None:
            return
        import shutil

        self.runner.mcp_config, self.runner.extra_args = self._saved
        shutil.rmtree(folder, ignore_errors=True)

    def _live_log(self, request, bindings):
        return _live_log_for(self.provider, bindings, request.attempt_id)

    def execute(self, request):
        self._checks_folder = None
        try:
            return self._execute(request)
        finally:
            self._detach_checks(self._checks_folder)

    def _execute(self, request):
        import json

        from agent_dev.adapters.role_execution import capture, enforce

        context = json.loads(request.context)
        bindings = context.get("repositories", {})
        role_policy = context.get("role_policy", {})
        if not role_policy:
            from agent_dev.domain.role_policy import legacy_role, policy

            canonical = legacy_role(
                {"id": request.phase_id, "capabilities": list(request.capabilities)}
            )
            role_policy = policy(
                canonical,
                project_paths=context.get("write_scope", [])
                or list(
                    dict.fromkeys(
                        p for binding in bindings.values() for p in binding.get("allowed_paths", [])
                    )
                ),
            )
        before = capture(bindings)
        self.runner.timeout_seconds = request.timeout_seconds
        task = Task.from_dict(request.task.definition)
        code_allowed = (
            "code.write" in request.capabilities and role_policy.get("mode", "code") == "code"
        )
        agent = AgentConfig(
            request.phase_id, request.phase_id, "", code_allowed, "Approved snapshot phase"
        )
        step = PipelineStep(
            request.phase_id,
            request.phase_id,
            f"outputs/{request.phase_id}-output.md",
            f"prompts/{request.phase_id}-prompt.md",
        )
        if self.provider == "claude_cli":
            tools = "Read,Grep,Glob,Edit,Write" if code_allowed else "Read,Grep,Glob"
            self.runner.extra_args[-1] = tools
            # Settings are not loaded, so "dontAsk" would deny every Edit/Write call. Code roles
            # auto-accept edits; their paths are still checked against the role policy below.
            self.runner.permission_mode = "acceptEdits" if code_allowed else "dontAsk"
        self._checks_folder = self._attach_checks(code_allowed)
        if hasattr(self.runner, "sandbox"):
            self.runner.sandbox = "workspace-write" if code_allowed else "read-only"
        # Only this provider's own session ids are resumable; anything else starts fresh.
        prefix = self.provider + ":"
        resumable = hasattr(self.runner, "resume_session_id")
        resume_id = (
            request.resume_session[len(prefix) :]
            if resumable and request.resume_session.startswith(prefix)
            else ""
        )
        if resumable:
            self.runner.resume_session_id = resume_id
        self.runner.live_log = self._live_log(request, bindings)
        try:
            try:
                prompt = resumed_context(request.context) if resume_id else request.context
                output = self.runner.run(task, agent, step, prompt)
            except (ClaudeResumeUnavailableError, CodexResumeUnavailableError):
                # The conversation is gone: start over with the full context.
                self.runner.resume_session_id = ""
                output = self.runner.run(task, agent, step, request.context)
        except (ClaudeUsageLimitError, CodexUsageLimitError, GeminiUsageLimitError):
            raise DomainError(ErrorCode.CAPACITY_EXHAUSTED, "Runner quota exhausted.") from None
        except subprocess.TimeoutExpired:
            raise DomainError(ErrorCode.EXTERNAL_TRANSIENT, "Runner timed out.") from None
        except (RuntimeError, OSError, ValueError) as exc:
            text = str(exc).lower()
            if "model" in text and "not supported" in text:
                raise DomainError(
                    ErrorCode.CAPABILITY_UNAVAILABLE,
                    "Configured model is unsupported by this CLI account. Select a supported model in the project profile and approve a new task.",
                ) from None
            if any(
                marker in text
                for marker in ("not logged in", "unauthorized", "authentication", "login required")
            ):
                raise DomainError(
                    ErrorCode.AUTH_REQUIRED, "Runner requires authentication."
                ) from None
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "Runner execution failed; inspect restricted local evidence.",
            ) from None
        if len(output) > 2_000_000:
            raise DomainError(ErrorCode.INVALID_INPUT, "Runner response exceeds evidence limit.")
        if role_policy:
            direct_policy = dict(role_policy, mode="readonly") if not code_allowed else role_policy
            enforce(bindings, before, direct_policy, context.get("node_config", {}).get("module"))
        else:
            # Legacy code.write is only a compatibility input; inspect this attempt's delta.
            legacy_policy = {"mode": "code" if code_allowed else "readonly", "scopes": []}
            enforce(bindings, before, legacy_policy)
        session = getattr(self.runner, "last_session_id", "") if resumable else ""
        if resumable:
            self.runner.resume_session_id = ""
        return PhaseResult(
            output,
            self.provider,
            getattr(self.runner, "last_usage", {}),
            getattr(self.runner, "last_selection", {}),
            resume_session=prefix + session if session else "",
        )
