"""Claude code roles must be able to write: no settings are loaded, so dontAsk denies edits."""

import json
import subprocess

import pytest

pytest_plugins = ["test_workflow_runtime"]

from test_workflow_dag import prepare
from test_workflow_session_execution import graph

from agent_dev.domain.contracts import PhaseRequest
from agent_dev.domain.role_policy import policy, session_policy


@pytest.mark.parametrize(
    ("members", "mode", "tools"),
    [
        (["planner"], "dontAsk", "Read,Grep,Glob"),
        (["developer"], "acceptEdits", "Read,Grep,Glob,Edit,Write"),
    ],
)
def test_claude_permission_mode_follows_write_capability(
    runtime, tmp_path, monkeypatch, members, mode, tools
):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter

    prepare(runtime, graph())
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    union = session_policy([policy(m, project_paths=["source.txt"]) for m in members])
    context = {
        "repositories": {"source": {"worktree": str(worktree), "allowed_paths": ["source.txt"]}},
        "role_policy": union,
        "node_config": {},
    }
    adapter = CliRunnerAdapter("claude_cli", {"process_env": {"PATH": "/bin"}}, tmp_path)
    seen = []

    def run(*args):
        seen.append((adapter.runner.permission_mode, adapter.runner.extra_args[-1]))
        return '```json\n{"handoff_status":"pass"}\n```'

    monkeypatch.setattr(adapter.runner, "run", run)
    request = PhaseRequest(
        runtime.store.task("DAG"),
        members[0],
        "fixture",
        "digest",
        0,
        frozenset(union["capabilities"]),
        json.dumps(context),
    )
    adapter.execute(request)
    assert seen == [(mode, tools)]
