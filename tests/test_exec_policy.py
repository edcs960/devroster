"""Child process environments and time limits (audit S-04, S-08; architecture step 3)."""

import json
import subprocess
import sys

import pytest

from agent_dev.adapters import process_launcher
from agent_dev.domain import exec_policy
from tests.test_execution_view import (
    Runner,
    runtime,  # noqa: F401 - fixture
)

SHELL = {
    "PATH": "/usr/bin",
    "HOME": "/home/me",
    "LC_CTYPE": "UTF-8",
    "JAVA_HOME": "/opt/jdk",
    "HTTPS_PROXY": "http://proxy:3128",
    "AWS_SECRET_ACCESS_KEY": "cloud-secret",
    "GITHUB_TOKEN": "ghp-secret",
    "DATABASE_URL": "postgres://approved",
}


@pytest.mark.parametrize("kind", ["hook", "mcp_stdio", "check", "preview"])
def test_project_commands_never_get_unlisted_secrets(kind):
    env = exec_policy.environment(kind, SHELL)
    assert env["PATH"] == "/usr/bin" and env["LC_CTYPE"] == "UTF-8"
    assert "AWS_SECRET_ACCESS_KEY" not in env and "GITHUB_TOKEN" not in env and "DATABASE_URL" not in env


def test_checks_keep_toolchains_proxy_and_approved_keys():
    env = exec_policy.environment("check", SHELL, allowed=["DATABASE_URL"])
    assert env["JAVA_HOME"] == "/opt/jdk" and env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["DATABASE_URL"] == "postgres://approved" and "AWS_SECRET_ACCESS_KEY" not in env
    hook = exec_policy.environment("hook", SHELL, extra={"DEVROSTER_EVENT": "after_edit"})
    assert "JAVA_HOME" not in hook and hook["DEVROSTER_EVENT"] == "after_edit"


def test_git_keeps_the_users_credentials_but_never_prompts():
    env = exec_policy.environment("git", SHELL)
    assert env["GITHUB_TOKEN"] == "ghp-secret" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert exec_policy.timeout("git") == 300


def test_code_role_checks_run_without_the_users_secrets(tmp_path, monkeypatch):
    """S-04: a check runs code the AI wrote; it must not see cloud keys or tokens."""
    from agent_dev.adapters.check_server import run_check

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://approved")
    probe = "import os,json;print(json.dumps(sorted(k for k in ('AWS_SECRET_ACCESS_KEY','DATABASE_URL','PATH') if k in os.environ)))"
    text = run_check(
        {"name": "unit", "command": [sys.executable, "-c", probe], "cwd": str(tmp_path), "environment_keys": ["DATABASE_URL"]}
    )
    assert "exit code: 0" in text
    assert json.loads(text.strip().splitlines()[-1]) == ["DATABASE_URL", "PATH"]


def test_verification_gates_get_the_check_environment(runtime, monkeypatch):  # noqa: F811
    from agent_dev.adapters import verification

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-secret")
    seen = []
    original = verification.run_bounded

    def capture(command, *, env, **options):
        seen.append(dict(env))
        return original(command, env=env, **options)

    monkeypatch.setattr(verification, "run_bounded", capture)
    runtime.execute("T", "run-once", runner=Runner({"input_tokens": 1, "output_tokens": 1}))
    result = runtime.execute("T", "verify-once", verify=True)
    assert result["passed"], result
    assert seen and all("AWS_SECRET_ACCESS_KEY" not in env and "PATH" in env for env in seen)
    assert all(env.get("CODEX_HOME") for env in seen)  # The app-private CLI homes stay isolated.


def test_git_calls_have_a_time_limit_and_report_it(monkeypatch, tmp_path):
    from agent_dev.scm import git_publish

    calls = []

    def fake(argv, **options):
        calls.append(options)
        raise subprocess.TimeoutExpired(argv, options["timeout"])

    monkeypatch.setattr(process_launcher.subprocess, "run", fake)
    with pytest.raises(RuntimeError, match="300 seconds"):
        git_publish.git_output(tmp_path, ["status"])
    assert calls[0]["timeout"] == 300 and calls[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
