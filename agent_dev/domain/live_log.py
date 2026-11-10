"""One readable line per AI CLI event for the live log; None for what is not shown.

The live log shows what the tool is doing (files read or changed, commands, messages), never
the model's internal reasoning: Codex `reasoning` items and Claude `thinking` blocks are
dropped here, so they are never written to disk.
"""

import json

TEXT_LIMIT = 300


def _short(value, limit=TEXT_LIMIT):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _codex(event):
    kind = event.get("type", "")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = item.get("type", "")
    if kind == "thread.started":
        return "세션 시작"
    if kind == "turn.completed":
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        tokens = sum(v for v in usage.values() if isinstance(v, int) and not isinstance(v, bool))
        return f"응답 완료 (토큰 {tokens:,})" if tokens else "응답 완료"
    if kind == "turn.failed" or kind == "error":
        message = event.get("message") or (event.get("error") or {}).get("message", "")
        return "오류: " + _short(message)
    if item_type == "command_execution":
        if kind == "item.started":
            return "명령 실행: " + _short(item.get("command"))
        if kind == "item.completed":
            code = item.get("exit_code")
            return f"명령 끝 (종료 코드 {code})" if code is not None else None
    if item_type == "file_change" and kind == "item.completed":
        changes = item.get("changes")
        changes = changes if isinstance(changes, list) else []
        paths = [c.get("path", "") for c in changes if isinstance(c, dict)]
        return "파일 변경: " + _short(", ".join(p for p in paths if p)) if paths else "파일 변경"
    if item_type == "agent_message" and kind == "item.completed":
        return "메시지: " + _short(item.get("text"))
    if item_type == "mcp_tool_call" and kind == "item.started":
        return "도구: " + _short(f"{item.get('server', '')}.{item.get('tool', '')}")
    return None


TOOL_TARGET = ("file_path", "path", "pattern", "command", "url")


def _claude(event):
    kind = event.get("type", "")
    if kind == "system" and event.get("subtype") == "init":
        return "세션 시작"
    if kind == "result":
        if event.get("is_error"):
            return "오류: " + _short(event.get("result"))
        return "응답 완료"
    if kind != "assistant":
        return None
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    lines = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            args = block.get("input")
            args = args if isinstance(args, dict) else {}
            target = next((args[k] for k in TOOL_TARGET if isinstance(args.get(k), str)), "")
            lines.append(_short(f"도구: {block.get('name', '')} {target}".strip()))
        elif block.get("type") == "text" and str(block.get("text", "")).strip():
            lines.append("메시지: " + _short(block["text"]))
        # "thinking" blocks are the model's reasoning: never shown or stored.
    return "\n".join(lines) or None


def summarize(provider, stream, line):
    """A short line for one output line of ``provider``'s CLI, or None."""
    text = line.rstrip("\r\n")
    if not text.strip():
        return None
    if stream == "stderr":
        # Progress and warnings; Codex also writes its own banner here.
        return "[알림] " + _short(text)
    try:
        event = json.loads(text)
    except ValueError:
        return _short(text)
    if not isinstance(event, dict):
        return None
    if provider == "codex_cli":
        return _codex(event)
    if provider == "claude_cli":
        return _claude(event)
    return None
