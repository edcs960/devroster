"""ADR 0002 W4: the session that owns a fix resumes its earlier conversation."""

import json

import pytest

pytest_plugins = ["test_workflow_runtime"]

from test_workflow_dag import prepare
from test_workflow_session_execution import ISSUE, graph
from test_workflow_sessions import SessionRunner

from agent_dev.core.models import AgentConfig, PipelineStep, Task
from agent_dev.domain.contracts import PhaseRequest, PhaseResult
from agent_dev.domain.role_policy import session_policy


class Resuming(SessionRunner):
    """Returns a new resume token per invocation and records the token it was given."""

    def __init__(self, issue=None):
        super().__init__(issue)
        self.tokens = []

    def execute(self, request):
        self.tokens.append(request.resume_session)
        result = super().execute(request)
        return PhaseResult(
            result.output, result.provider, result.usage, resume_session=f"fake:{len(self.tokens)}"
        )


def test_reentry_resumes_the_fixing_session_and_verifies_fresh(runtime):
    prepare(runtime, graph())
    runner = Resuming(issue={"qa": ISSUE})
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    assert runner.calls == [
        ("planner", "architect"),
        ("developer",),
        ("qa", "reviewer"),
        ("developer",),
        ("qa", "reviewer"),
    ]
    # Second develop invocation continues the first develop conversation (call 2);
    # the rerun verification starts a new conversation.
    assert runner.tokens == ["", "", "", "fake:2", ""]


def test_no_resume_without_reentry_or_token(runtime):
    prepare(runtime, graph())
    runner = SessionRunner(issue={"qa": ISSUE})  # never returns a resume token
    tokens = []
    execute = runner.execute
    runner.execute = lambda request: tokens.append(request.resume_session) or execute(request)
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    assert tokens == ["", "", "", "", ""]


def adapter_request(runtime, token):
    prepare(runtime, graph())
    context = {"repositories": {}, "role_policy": session_policy([]), "node_config": {}}
    return PhaseRequest(
        runtime.store.task("DAG"),
        "planner",
        "fixture",
        "digest",
        1,
        frozenset({"code.read"}),
        json.dumps(context),
        node_ids=("planner",),
        resume_session=token,
    )


@pytest.mark.parametrize(
    ("token", "expected"),
    [("claude_cli:abc", "abc"), ("codex_cli:abc", ""), ("", "")],
)
def test_claude_adapter_resumes_only_its_own_sessions(
    runtime, tmp_path, monkeypatch, token, expected
):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter

    adapter = CliRunnerAdapter("claude_cli", {"process_env": {"PATH": "/bin"}}, tmp_path)
    seen = []

    def run(*args):
        seen.append(adapter.runner.resume_session_id)
        adapter.runner.last_session_id = "next"
        return '```json\n{"handoff_status":"pass"}\n```'

    monkeypatch.setattr(adapter.runner, "run", run)
    result = adapter.execute(adapter_request(runtime, token))
    assert seen == [expected]
    assert result.resume_session == "claude_cli:next"
    assert adapter.runner.resume_session_id == ""


def test_claude_adapter_starts_fresh_when_conversation_is_gone(runtime, tmp_path, monkeypatch):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter
    from agent_dev.runners.claude_cli import ClaudeResumeUnavailableError

    adapter = CliRunnerAdapter("claude_cli", {"process_env": {"PATH": "/bin"}}, tmp_path)
    seen = []

    def run(*args):
        seen.append(adapter.runner.resume_session_id)
        if adapter.runner.resume_session_id:
            raise ClaudeResumeUnavailableError("No conversation found with session ID: gone")
        adapter.runner.last_session_id = "fresh"
        return '```json\n{"handoff_status":"pass"}\n```'

    monkeypatch.setattr(adapter.runner, "run", run)
    result = adapter.execute(adapter_request(runtime, "claude_cli:gone"))
    assert seen == ["gone", ""]
    assert result.resume_session == "claude_cli:fresh"


def test_claude_runner_passes_resume_and_reads_session_id(tmp_path, monkeypatch):
    from agent_dev.runners import claude_cli

    monkeypatch.setattr(claude_cli, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}')
    calls = []

    class Process:
        pid = 123456789

        def __init__(self, command, **kwargs):
            calls.append(command)
            self.returncode = 1 if "missing" in command else 0

        def communicate(self, **kwargs):
            if self.returncode:
                return "", "No conversation found with session ID: missing"
            return json.dumps({"result": "done", "session_id": "s-2", "usage": {}}), ""

    monkeypatch.setattr("subprocess.Popen", Process)
    runner = claude_cli.ClaudeCliRunner(
        {
            "load_env_files": False,
            "inline_prompt": True,
            "collect_usage": True,
            "output_format": "json",
            "workspace_path": str(tmp_path),
            "process_registry_path": str(tmp_path / "registry.json"),
        }
    )
    runner._help_text = "--setting-sources"
    task = Task.from_dict({"id": "T", "title": "t"})
    step = PipelineStep("developer", "dev", "output.md", "prompt.md")
    agent = AgentConfig("dev", "dev", "", True, "")

    runner.resume_session_id = "s-1"
    assert runner.run(task, agent, step, "prompt") == "done"
    assert calls[-1][calls[-1].index("--resume") + 1] == "s-1"
    assert runner.last_session_id == "s-2"

    runner.resume_session_id = ""
    runner.run(task, agent, step, "prompt")
    assert "--resume" not in calls[-1]

    runner.resume_session_id = "missing"
    with pytest.raises(claude_cli.ClaudeResumeUnavailableError):
        runner.run(task, agent, step, "prompt")
    assert runner.last_session_id == ""


def test_dag_keeps_full_context_for_a_resumed_session(runtime):
    prepare(runtime, graph())
    contexts = []

    class Recording(Resuming):
        def execute(self, request):
            contexts.append(json.loads(request.context))
            return super().execute(request)

    runner = Recording(issue={"qa": ISSUE})
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    # The runner may fall back to another provider or a fresh conversation, so the
    # trimming happens only where the conversation really continues.
    assert runner.tokens[3] == "fake:2"
    assert "operating_rules" in contexts[3] and "resumed_session" not in contexts[3]


def context_request(runtime, token):
    prepare(runtime, graph())
    context = {
        "operating_rules": "rules",
        "approved_design": "design",
        "repositories": {},
        "role_policy": session_policy([]),
        "node_config": {},
    }
    return PhaseRequest(
        runtime.store.task("DAG"),
        "planner",
        "fixture",
        "digest",
        1,
        frozenset({"code.read"}),
        json.dumps(context),
        node_ids=("planner",),
        resume_session=token,
    )


@pytest.mark.parametrize("provider", ["claude_cli", "codex_cli"])
@pytest.mark.parametrize(
    ("owner", "resumed"), [("claude_cli", "claude_cli"), ("codex_cli", "codex_cli"), ("", "")]
)
def test_only_the_resuming_runner_trims_context(
    runtime, tmp_path, monkeypatch, provider, owner, resumed
):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter

    adapter = CliRunnerAdapter(provider, {"process_env": {"PATH": "/bin"}}, tmp_path)
    seen = []

    def run(task, agent, step, prompt):
        seen.append((adapter.runner.resume_session_id, json.loads(prompt)))
        adapter.runner.last_session_id = "next"
        return '```json\n{"handoff_status":"pass"}\n```'

    monkeypatch.setattr(adapter.runner, "run", run)
    token = owner + ":abc" if owner else ""
    result = adapter.execute(context_request(runtime, token))
    (resume_id, prompt), own = seen[0], resumed == provider
    assert resume_id == ("abc" if own else "")
    assert ("operating_rules" in prompt) is not own
    assert ("resumed_session" in prompt) is own
    assert result.resume_session == provider + ":next"


def test_codex_adapter_starts_fresh_with_full_context_when_thread_is_gone(
    runtime, tmp_path, monkeypatch
):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter
    from agent_dev.runners.codex_cli import CodexResumeUnavailableError

    adapter = CliRunnerAdapter("codex_cli", {"process_env": {"PATH": "/bin"}}, tmp_path)
    seen = []

    def run(task, agent, step, prompt):
        seen.append((adapter.runner.resume_session_id, "operating_rules" in json.loads(prompt)))
        if adapter.runner.resume_session_id:
            raise CodexResumeUnavailableError("no rollout found for thread id gone")
        adapter.runner.last_session_id = "fresh"
        return '```json\n{"handoff_status":"pass"}\n```'

    monkeypatch.setattr(adapter.runner, "run", run)
    result = adapter.execute(context_request(runtime, "codex_cli:gone"))
    assert seen == [("gone", False), ("", True)]
    assert result.resume_session == "codex_cli:fresh"


def test_codex_runner_resumes_with_the_same_sandbox(tmp_path, monkeypatch):
    from agent_dev.runners import codex_cli

    calls = []
    extra = tmp_path / "other"

    class Process:
        pid = 123456789

        def __init__(self, command, **kwargs):
            calls.append((command, kwargs["cwd"]))
            self.returncode = 1 if "missing" in command else 0

        def communicate(self, **kwargs):
            if self.returncode:
                return "", "Error: thread/resume failed: no rollout found for thread id missing"
            return '{"type":"thread.started","thread_id":"t-2"}\n{"type":"turn.completed"}', ""

    monkeypatch.setattr("subprocess.Popen", Process)
    runner = codex_cli.CodexCliRunner(
        {
            "load_env_files": False,
            "inline_prompt": True,
            "collect_usage": True,
            "inject_shared_mcp": False,
            "sandbox": "read-only",
            "workspace_path": str(tmp_path),
            "process_registry_path": str(tmp_path / "registry.json"),
        }
    )
    runner._exec_help = ""
    runner.add_dirs = [extra]
    task = Task.from_dict({"id": "T", "title": "t"})
    step = PipelineStep("developer", "dev", "output.md", "prompt.md")
    agent = AgentConfig("dev", "dev", "", True, "")

    runner.run(task, agent, step, "prompt")
    command, cwd = calls[-1]
    assert command[1:3] == ["exec", "-C"] and "--add-dir" in command
    assert runner.last_session_id == "t-2"

    runner.resume_session_id = "t-2"
    runner.run(task, agent, step, "prompt")
    command, cwd = calls[-1]
    assert command[1:4] == ["exec", "resume", "t-2"] and cwd == tmp_path.resolve()
    assert "-C" not in command and "--sandbox" not in command and "--add-dir" not in command
    assert 'sandbox_mode="read-only"' in command
    assert f'sandbox_workspace_write.writable_roots=["{extra}"]' in command
    assert command[-1].endswith("prompt")

    runner.resume_session_id = "missing"
    with pytest.raises(codex_cli.CodexResumeUnavailableError):
        runner.run(task, agent, step, "prompt")
    assert runner.last_session_id == ""


@pytest.mark.parametrize(("limit", "expected"), [(0, 3600), (600, 600)])
def test_session_timeout_caps_one_invocation(runtime, limit, expected):
    profile = json.loads(
        runtime.store.db.rows(
            "SELECT content_json FROM project_profile_versions ORDER BY version DESC LIMIT 1"
        )[0]["content_json"]
    )
    # Same steps as test_workflow_dag.prepare, with the session limit in that one revision.
    profile["workflow"] = {"document": graph()}
    profile["allowed_paths"] = ["source.txt"]
    profile["execution_limits"] = {"session_timeout_seconds": limit}
    runtime.store.revise_profile("P", profile, 0)
    runtime.store.create_plan("P", "PLAN2", "D", 1, [{"id": "DAG", "title": "Implement"}])
    runtime.store.approve("plan", "PLAN2", 1, "user")
    runtime.store.create_snapshot("DAG", runtime.resolve_snapshot("DAG"))
    seen = []

    class Timing(SessionRunner):
        def execute(self, request):
            seen.append((request.node_ids, request.timeout_seconds))
            return super().execute(request)

    runtime.execute("DAG", "run", runner=Timing())
    assert seen[0] == (("planner", "architect"), expected)
