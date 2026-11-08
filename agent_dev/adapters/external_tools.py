"""Concrete GitHub, Figma, Playwright, Semgrep and ZAP boundaries."""

import json
import os
import signal
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from agent_dev.domain.errors import DomainError, ErrorCode


class ProcessOperation:
    def __init__(self, command, cwd, *, env=None, input_text=None):
        self.command, self.cwd, self.env, self.input_text = (
            list(command),
            Path(cwd),
            env,
            input_text,
        )

    def execute(self, timeout_seconds):
        try:
            process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.env,
                text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE, "Tool executable unavailable."
            ) from None
        try:
            stdout, stderr = process.communicate(self.input_text, timeout=timeout_seconds)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise DomainError(
                ErrorCode.EXTERNAL_TRANSIENT, "Tool execution cancelled or timed out."
            ) from None
        if process.returncode:
            code = (
                ErrorCode.AUTH_REQUIRED
                if any(
                    s in (stdout + stderr).lower() for s in ("not logged in", "unauthorized", "401")
                )
                else ErrorCode.EXTERNAL_TRANSIENT
            )
            raise DomainError(code, "External tool failed; restricted local evidence required.")
        if len(stdout) > 2_000_000:
            raise DomainError(ErrorCode.INVALID_INPUT, "Tool response exceeds evidence limit.")
        return stdout


class JsonReadOperation:
    def __init__(self, url, credential_ref, credential_resolver):
        self.url, self.ref, self.resolve = url, credential_ref, credential_resolver

    def execute(self, timeout_seconds):
        token = self.resolve(self.ref)
        if not token:
            raise DomainError(ErrorCode.AUTH_REQUIRED, "Credential reference unavailable.")
        request = urllib.request.Request(self.url, headers={"X-Figma-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            raise DomainError(
                ErrorCode.AUTH_REQUIRED if exc.code in {401, 403} else ErrorCode.EXTERNAL_TRANSIENT,
                "Design service request failed.",
            ) from None
        except OSError:
            raise DomainError(ErrorCode.EXTERNAL_TRANSIENT, "Design service unavailable.") from None
        if len(raw) > 2_000_000:
            raise DomainError(ErrorCode.INVALID_INPUT, "Design response exceeds evidence limit.")
        return json.loads(raw)


class ExternalToolAdapters:
    def __init__(self, gateway, cwd, credential_resolver=lambda ref: os.environ.get(ref, "")):
        self.gateway, self.cwd, self.credentials = gateway, Path(cwd), credential_resolver

    def github_read(self, connection, repository, endpoint="pulls"):
        if endpoint not in {"pulls", "actions/runs"} or "/" not in repository:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unsupported GitHub query.")
        return self.gateway.call(
            connection,
            "github.read",
            repository,
            ProcessOperation(["gh", "api", f"repos/{repository}/{endpoint}"], self.cwd),
        )

    def figma_read(self, connection, file_key):
        if not file_key.isalnum():
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid Figma file key.")
        return self.gateway.call(
            connection,
            "design.read",
            file_key,
            JsonReadOperation(
                "https://api.figma.com/v1/files/" + file_key,
                connection.credential_ref,
                self.credentials,
            ),
        )

    def figma_write(self, connection, file_key, operation, approval_id, *, task_id, phase_id):
        return self.gateway.call(
            connection,
            "design.write",
            file_key,
            operation,
            approval_id=approval_id,
            task_id=task_id,
            phase_id=phase_id,
        )

    def playwright(self, connection, url, destination):
        destination = Path(destination).resolve()
        if not destination.is_relative_to(self.cwd.resolve()):
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Browser evidence must stay in its worktree."
            )
        # Use an installed browser tool; never install code while running a task.
        return self.gateway.call(
            connection,
            "browser.test",
            url,
            ProcessOperation(["playwright", "screenshot", url, str(destination)], self.cwd),
            timeout_seconds=120,
        )

    def semgrep(self, connection, config="auto"):
        if config == "auto":
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Semgrep needs a registered local rules file."
            )
        rules = (self.cwd / config).resolve()
        if not rules.is_relative_to(self.cwd.resolve()) or not rules.is_file():
            raise DomainError(ErrorCode.INVALID_INPUT, "Security rules file unavailable.")
        return self.gateway.call(
            connection,
            "security.sast",
            str(self.cwd.resolve()),
            ProcessOperation(
                ["semgrep", "scan", "--config", str(rules), "--json", "--error", "."], self.cwd
            ),
            timeout_seconds=300,
        )

    def zap(self, connection, url, approval_id, *, task_id, phase_id):
        return self.gateway.call(
            connection,
            "security.active_scan",
            url,
            ProcessOperation(["zap-cli", "quick-scan", "--self-contained", url], self.cwd),
            approval_id=approval_id,
            task_id=task_id,
            phase_id=phase_id,
            timeout_seconds=300,
        )
