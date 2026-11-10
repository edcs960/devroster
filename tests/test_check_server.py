"""Claude code roles can run only the approved verification commands, through a one-tool MCP server."""

import json
import subprocess
import sys

from agent_dev.adapters.check_server import TOOL
from agent_dev.adapters.cli_runner import CliRunnerAdapter
from agent_dev.adapters.workflow_runtime import project_checks


def server(tmp_path, checks, max_calls=10):
    config = tmp_path / "checks.json"
    config.write_text(json.dumps({"checks": checks, "max_calls": max_calls}), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "agent_dev.adapters.check_server", str(config)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )


def ask(process, ident, method, params=None):
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}) + "\n")
    process.stdin.flush()
    return json.loads(process.stdout.readline())


def test_only_listed_gates_run_with_their_fixed_command(tmp_path):
    checks = [{"name": "unit", "command": [sys.executable, "-c", "print('ok from gate')"], "cwd": str(tmp_path), "timeout": 30}]
    process = server(tmp_path, checks, max_calls=1)
    try:
        assert ask(process, 1, "initialize", {"protocolVersion": "2025-06-18"})["result"]["capabilities"] == {"tools": {}}
        tools = ask(process, 2, "tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == [TOOL] and tools[0]["inputSchema"]["properties"]["gate"]["enum"] == ["unit"]
        refused = ask(process, 3, "tools/call", {"name": TOOL, "arguments": {"gate": "rm -rf /"}})["result"]
        assert refused["isError"] and "unit" in refused["content"][0]["text"]
        ran = ask(process, 4, "tools/call", {"name": TOOL, "arguments": {"gate": "unit"}})["result"]
        assert not ran["isError"] and "exit code: 0" in ran["content"][0]["text"] and "ok from gate" in ran["content"][0]["text"]
        limited = ask(process, 5, "tools/call", {"name": TOOL, "arguments": {"gate": "unit"}})["result"]
        assert limited["isError"] and "횟수" in limited["content"][0]["text"]
        assert "error" in ask(process, 6, "resources/list")
    finally:
        process.stdin.close()
        process.wait(timeout=10)


def test_gates_are_bound_to_the_task_worktree_and_cannot_leave_it(tmp_path):
    worktree = tmp_path / "task" / "source"
    (worktree / "web").mkdir(parents=True)
    resolved = {
        "repositories": {"source": {"worktree": str(worktree)}},
        "verification": {"commands": [
            {"name": "unit", "repository": "source", "command": ["pytest"], "timeout_seconds": 60},
            {"name": "web", "repository": "source", "command": ["npm", "test"], "cwd": "web"},
            {"name": "escape", "repository": "source", "command": ["ls"], "cwd": "../.."},
            {"name": "other", "repository": "missing", "command": ["ls"]},
        ]},
    }
    checks = project_checks(resolved)
    assert [(c["name"], c["cwd"]) for c in checks] == [("unit", str(worktree.resolve())), ("web", str((worktree / "web").resolve()))]
    assert checks[0]["timeout"] == 60 and checks[1]["timeout"] == 900


def test_only_claude_code_roles_get_the_tool_and_only_for_one_call(tmp_path):
    checks = [{"name": "unit", "command": ["pytest"], "cwd": str(tmp_path), "timeout": 60}]
    adapter = CliRunnerAdapter("claude_cli", {"project_checks": checks}, tmp_path)
    before = (list(adapter.runner.mcp_config), list(adapter.runner.extra_args))
    assert adapter._attach_checks(False) is None
    folder = adapter._attach_checks(True)
    assert adapter.runner.extra_args[-2:] == ["--allowedTools", "mcp__devroster__run_project_check"]
    servers = json.loads((folder / "mcp.json").read_text(encoding="utf-8"))
    assert servers["mcpServers"]["devroster"]["args"][:2] == ["-m", "agent_dev.adapters.check_server"]
    adapter._detach_checks(folder)
    assert (list(adapter.runner.mcp_config), list(adapter.runner.extra_args)) == before and not folder.exists()
    codex = CliRunnerAdapter("codex_cli", {"project_checks": checks}, tmp_path)
    assert codex._attach_checks(True) is None  # Codex already has its own sandboxed shell.
