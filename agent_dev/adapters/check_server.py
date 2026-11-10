"""A one-tool MCP server (stdio) that runs the project's approved verification commands.

Claude and Gemini code roles have no shell, so they could only see test results after the
step, in DevRoster's own verification. This server lets them run exactly the approved
checks while they work: the gate list comes from the approved snapshot, the command, folder
and time limit cannot be changed by the model, and output is bounded and masked like the
verification step. It is started by the AI CLI for one step and exits with it.

    python check_server.py <config.json>
    config: {"checks": [{"name", "command": [...], "cwd", "timeout"}], "max_calls": 10}
"""

import json
import subprocess
import sys
from pathlib import Path

SERVER = {"name": "devroster-checks", "version": "1"}
TOOL = "run_project_check"
OUTPUT_TAIL = 6000
DEFAULT_PROTOCOL = "2025-06-18"


def load(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    checks = {c["name"]: c for c in config.get("checks", [])}
    return checks, int(config.get("max_calls", 10))


def tool_definition(checks):
    return {
        "name": TOOL,
        "description": (
            "Run one of this project's approved verification commands (tests, lint, build) in the "
            "task worktree and return its exit code and the end of its output. Only the listed "
            "gates can run; their commands and folders are fixed by the approved plan."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"gate": {"type": "string", "enum": sorted(checks)}},
            "required": ["gate"],
            "additionalProperties": False,
        },
    }


def run_check(check):
    from agent_dev.adapters.bounded_process import run_bounded
    from agent_dev.domain.content import redact

    try:
        process = run_bounded(
            check["command"], cwd=check["cwd"], env=None, timeout=max(1, int(check.get("timeout", 900))),
            limit=2_000_000, tail=OUTPUT_TAIL,
        )
        code = 125 if process.overflow else process.returncode
        output = process.output.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        code, output = 124, "제한 시간을 넘겨 중단했습니다."
    except OSError:
        code, output = 127, "검증 명령을 실행할 수 없습니다."
    return f"gate: {check['name']}\nexit code: {code}\n\n{redact(output)}"


def handle(message, checks, state):
    """The JSON-RPC response for one message, or None for a notification."""
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None
    if method == "initialize":
        version = (message.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [tool_definition(checks)]}
    elif method == "tools/call":
        params = message.get("params") or {}
        gate = (params.get("arguments") or {}).get("gate")
        if params.get("name") != TOOL or gate not in checks:
            text, error = "승인된 검증 명령 이름을 지정해주세요: " + ", ".join(sorted(checks)), True
        elif state["calls"] >= state["max_calls"]:
            text, error = "이 단계에서 검증 도구를 쓸 수 있는 횟수를 모두 썼습니다.", True
        else:
            state["calls"] += 1
            text, error = run_check(checks[gate]), False
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def main(argv=None, stdin=None, stdout=None):
    import io

    argv = sys.argv[1:] if argv is None else argv
    # JSON-RPC is UTF-8 whatever the console code page is (cp949, or C/ASCII locales).
    stdin = stdin or io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = stdout or io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n")
    checks, max_calls = load(argv[0])
    state = {"calls": 0, "max_calls": max_calls}
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(message, checks, state) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
