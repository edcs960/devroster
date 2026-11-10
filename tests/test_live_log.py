"""Live log: readable lines while an AI CLI runs, without model reasoning or secrets."""

import json
import subprocess
import sys
import time

import pytest
from test_budget_pause import Runner, runtime_with

from agent_dev.adapters import live_log as logs
from agent_dev.adapters.execution_view import live_log
from agent_dev.domain.live_log import summarize
from agent_dev.runners.common import stream_communicate
from agent_dev.runners.usage import claude_result, claude_session


def line(event):
    return json.dumps(event) + "\n"


def test_codex_events_become_short_lines_and_reasoning_is_dropped():
    assert summarize("codex_cli", "stdout", line({"type": "item.started", "item": {"type": "command_execution", "command": "pytest -q"}})) == "명령 실행: pytest -q"
    changed = {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "src/a.py"}, {"path": "src/b.py"}]}}
    assert summarize("codex_cli", "stdout", line(changed)) == "파일 변경: src/a.py, src/b.py"
    assert summarize("codex_cli", "stdout", line({"type": "item.completed", "item": {"type": "reasoning", "text": "secret plan"}})) is None
    assert summarize("codex_cli", "stdout", line({"type": "turn.completed", "usage": {"input_tokens": 1200, "output_tokens": 30}})) == "응답 완료 (토큰 1,230)"


def test_claude_stream_shows_tools_and_text_but_not_thinking():
    event = {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py", "old_string": "x"}},
        {"type": "text", "text": "수정했습니다."},
    ]}}
    assert summarize("claude_cli", "stdout", line(event)) == "도구: Edit src/a.py\n메시지: 수정했습니다."
    assert summarize("claude_cli", "stdout", line({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "x"}]}})) is None
    assert summarize("claude_cli", "stderr", "warning: slow\n") == "[알림] warning: slow"


def test_claude_final_result_and_session_come_from_the_stream():
    stream = "".join([
        line({"type": "system", "subtype": "init", "session_id": "S1"}),
        line({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}),
        line({"type": "result", "result": "DONE", "session_id": "S1", "usage": {"input_tokens": 10, "output_tokens": 2}}),
    ])
    result, usage = claude_result(stream)
    assert result == "DONE" and usage["input_tokens"] == 10 and claude_session(stream) == "S1"
    single = json.dumps({"result": "OLD", "session_id": "S0"})  # --output-format json still works
    assert claude_result(single)[0] == "OLD" and claude_session(single) == "S0"


def test_log_masks_secrets_caps_size_and_reads_by_offset(tmp_path, monkeypatch):
    path = tmp_path / ".attempts" / "A1.log"
    log = logs.LiveLog(path, "claude_cli", secrets=("tok-123456789",))
    log.write("stderr", "using tok-123456789 now\n")
    text, offset = logs.read(path)
    assert "tok-123456789" not in text and "[알림]" in text
    assert logs.read(path, offset) == ("", offset)
    monkeypatch.setattr(logs, "LIMIT_BYTES", offset + 40)
    for _ in range(5):
        log.write("stderr", "x" * 30 + "\n")
    assert "기록하지 않습니다" in logs.read(path)[0] and path.stat().st_size <= offset + 200


def test_old_logs_in_the_task_folder_are_removed(tmp_path):
    folder = tmp_path / ".attempts"
    folder.mkdir()
    old = folder / "OLD.log"
    old.write_text("x", encoding="utf-8")
    week = time.time() - 31 * 86400
    import os

    os.utime(old, (week, week))
    logs.LiveLog(folder / "NEW.log", "codex_cli")
    assert not old.exists() and (folder / "NEW.log").exists()


def test_logs_move_out_of_a_folder_being_removed_and_expire_later(tmp_path):
    import os

    attempts = tmp_path / "worktrees" / "T" / ".attempts"
    logs.LiveLog(attempts / "A1.log", "codex_cli").write("stderr", "kept line\n")
    (attempts / "A1.json").write_text("{}", encoding="utf-8")  # A baseline: stays behind.
    stale = time.time() - 40 * 86400
    os.utime(attempts / "A1.log", (stale, stale))  # Written long ago; retention restarts at the move.
    assert logs.archive(attempts) == 1
    kept = tmp_path / "worktrees" / ".logs" / "T" / "A1.log"
    assert kept == logs.archive_dir(attempts) / "A1.log" and "kept line" in logs.read(kept)[0]
    assert not (attempts / "A1.log").exists() and (attempts / "A1.json").exists()
    # The next attempt anywhere in the project sweeps the archive.
    logs.LiveLog(tmp_path / "worktrees" / "U" / ".attempts" / "B1.log", "codex_cli")
    assert kept.exists()
    os.utime(kept, (stale, stale))
    logs.LiveLog(tmp_path / "worktrees" / "U" / ".attempts" / "B2.log", "codex_cli")
    assert not kept.exists() and not kept.parent.exists()


def test_lines_arrive_while_the_process_runs():
    script = "import sys,time\nfor i in range(3):\n    print('line', i, flush=True)\n    time.sleep(0.2)\nprint('err', file=sys.stderr)"
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    seen = []
    started = time.monotonic()
    stdout, stderr = stream_communicate(process, "", 10, lambda stream, text: seen.append((stream, text.strip(), time.monotonic() - started)))
    assert stdout.splitlines() == ["line 0", "line 1", "line 2"] and stderr.strip() == "err"
    first = next(t for s, text, t in seen if text == "line 0")
    assert first < 0.35  # Received before the process finished (~0.6s).


def test_a_hung_process_times_out_and_is_stopped():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    with pytest.raises(subprocess.TimeoutExpired):
        stream_communicate(process, "", 0.5, lambda *a: None)
    assert process.poll() is not None


def test_log_api_follows_the_latest_step(tmp_path):
    runtime = runtime_with(tmp_path, {"estimated_budget": 100.0, "price_book": []})
    runtime.execute("T", "run-1", runner=Runner())
    bindings = runtime.store.snapshot("T")["resolved"]["repositories"]
    attempts = runtime.store.db.rows("SELECT id,phase_id FROM phase_attempts WHERE task_id='T' ORDER BY started_at DESC, id DESC")
    latest = attempts[0]
    log = logs.LiveLog(logs.log_path(bindings, latest["id"]), "codex_cli")
    log.write("stdout", line({"type": "item.started", "item": {"type": "command_execution", "command": "npm test"}}))
    first = live_log(runtime.root, "T")
    assert first["attempt_id"] == latest["id"] and "명령 실행: npm test" in first["text"]
    assert live_log(runtime.root, "T", first["offset"], latest["id"])["text"] == ""
    # A reader that was following an older step gets the newest step from its start.
    assert "npm test" in live_log(runtime.root, "T", first["offset"], attempts[1]["id"])["text"]
