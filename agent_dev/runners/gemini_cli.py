"""Gemini headless execution with explicit admin policy and isolated invocation settings."""

import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from agent_dev.core.cli_home import GEMINI_CONTEXT_FILE, isolated_environment
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.model_routing import select_model
from agent_dev.storage.process_registry import ProcessRegistry

READ_TOOLS = ["read_file", "read_many_files", "list_directory", "glob", "grep_search"]


class GeminiCliRunner:
    def __init__(self, config=None):
        config = config or {}
        self.command = config.get("command", "gemini")
        self.model = config.get("model", "")
        self.model_config = deepcopy(
            {k: config[k] for k in ("model", "effort", "routing") if k in config}
        )
        self.workspace_path = Path(config.get("workspace_path", ".")).resolve()
        self.process_env = isolated_environment(config.get("process_env", os.environ))
        self.timeout_seconds = config.get("timeout_seconds", 1800)
        self.process_registry_path = config.get("process_registry_path")
        self.add_dirs, self.extra_args = [], []
        self.lease_config = None
        self.no_tools = config.get("no_tools", False)
        self.last_usage, self.last_selection = {}, {}

    def preflight(self):
        try:
            help_result = subprocess.run(
                [self.command, "--help"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=self.process_env,
            )
            if help_result.returncode or "--admin-policy" not in help_result.stdout:
                raise ValueError("Policy unavailable")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Gemini CLI의 --admin-policy 지원 버전이 필요합니다. CLI를 업데이트해주세요.",
            ) from None
        if os.name == "nt":
            directory = (
                Path(os.environ.get("ProgramData", "C:/ProgramData")) / "gemini-cli/policies"
            )
        else:
            directory = Path(
                "/Library/Application Support/GeminiCli/policies"
                if sys.platform == "darwin"
                else "/etc/gemini-cli/policies"
            )
        # Gemini ignores supplemental policy when central admin policy files exist.
        if directory.exists() and (not directory.is_dir() or any(directory.glob("*.toml"))):
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "중앙 Gemini 정책이 있어 작업별 도구 제한을 보장할 수 없습니다. 관리자가 승인한 CLI 구성을 사용해주세요.",
            )

    def run(self, task, agent, step, prompt):
        self.last_usage = {}
        self.last_selection = select_model(self.model_config, task.to_dict(), step.step_id)
        if self.last_selection["effort"]:
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "Gemini CLI의 추론 강도는 CLI 기본값을 사용합니다. 명시적 강도는 Gemini API를 선택해주세요.",
            )
        self.preflight()
        tools = (
            []
            if self.no_tools
            else READ_TOOLS + (["write_file", "replace"] if agent.code_allowed else [])
        )
        with tempfile.TemporaryDirectory(prefix="fa-gemini-") as folder:
            root = Path(folder)
            policy, settings = root / "policy.toml", root / "settings.json"
            # Highest supplemental admin tier denies every unknown or ambient tool.
            policy.write_text(
                '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 998\n'
                + (
                    "\n[[rule]]\ntoolName = "
                    + json.dumps(tools)
                    + '\ndecision = "allow"\npriority = 999\n'
                    if tools
                    else ""
                ),
                encoding="utf-8",
            )
            settings.write_text(
                json.dumps(
                    {
                        "admin": {"mcp": {"enabled": False}, "extensions": {"enabled": False}},
                        "hooksConfig": {"enabled": False},
                        "context": {
                            # Only the app's internal note (GEMINI_CLI_HOME/.gemini); never
                            # the project's GEMINI.md/AGENTS.md.
                            "fileName": [GEMINI_CONTEXT_FILE],
                            "includeDirectories": [str(p) for p in self.add_dirs],
                            "loadFromIncludeDirectories": False,
                        },
                        "tools": {"core": tools, "discoveryCommand": "", "callCommand": ""},
                        "general": {"enableAutoUpdate": False},
                        "privacy": {"usageStatisticsEnabled": False},
                    }
                ),
                encoding="utf-8",
            )
            policy.chmod(0o600)
            settings.chmod(0o600)
            env = dict(
                self.process_env,
                GEMINI_CLI_SYSTEM_SETTINGS_PATH=str(settings),
                GEMINI_CLI_SYSTEM_DEFAULTS_PATH=str(settings),
                GEMINI_CLI_TRUST_WORKSPACE="true",
            )
            command = [
                self.command,
                "--output-format",
                "json",
                "--approval-mode",
                "default",
                "--admin-policy",
                str(policy),
                "--prompt",
                "Follow the approved input from stdin.",
            ]
            if self.last_selection["model"]:
                command += ["--model", self.last_selection["model"]]
            for directory in self.add_dirs:
                command += ["--include-directories", str(directory)]
            if self.lease_config:
                from agent_dev.adapters import process_guard

                grant = self.lease_config
                command = [
                    sys.executable,
                    process_guard.__file__,
                    "--parent",
                    str(os.getpid()),
                    "--db",
                    grant["db"],
                    "--resource",
                    grant["resource"],
                    "--holder",
                    grant["holder"],
                    "--token",
                    str(grant["token"]),
                    "--",
                    *command,
                ]
            process = subprocess.Popen(
                command,
                cwd=self.workspace_path,
                env=env,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            registry = (
                ProcessRegistry(Path(self.process_registry_path))
                if self.process_registry_path
                else ProcessRegistry()
            )
            registry.register(
                task_id=task.task_id,
                phase=step.step_id,
                pid=process.pid,
                command=command,
                kind="gemini_cli",
            )
            try:
                stdout, stderr = process.communicate(input=prompt, timeout=self.timeout_seconds)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise
            finally:
                registry.unregister(process.pid)
            if process.returncode:
                from agent_dev.runners.common import GeminiUsageLimitError, is_usage_limit_message

                if is_usage_limit_message(stderr + stdout):
                    raise GeminiUsageLimitError("Gemini CLI quota exhausted.")
                code = ErrorCode.POLICY_VIOLATION
                raise DomainError(
                    code, "Gemini CLI 실행에 실패했습니다. 인증·모델·버전·사용 한도를 확인해주세요."
                )
            try:
                if len(stdout) > 2_000_000:
                    raise ValueError("Response too large")
                data = json.loads(stdout)
                if (
                    data.get("error")
                    or not isinstance(data.get("response"), str)
                    or not data["response"].strip()
                ):
                    raise ValueError("Invalid response")
            except (ValueError, AttributeError):
                raise DomainError(
                    ErrorCode.INVALID_INPUT,
                    "Gemini CLI 출력이 비어 있거나 JSON 형식이 잘못되었습니다.",
                ) from None
            return data["response"].strip()
