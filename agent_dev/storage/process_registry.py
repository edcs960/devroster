from __future__ import annotations

import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_dev.core.io import locked, read_json, write_json
from agent_dev.core.paths import DATA_DIR
from agent_dev.core.timezones import KST


class ProcessRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "process_registry.json"
        if not self.path.exists():
            write_json(self.path, {"processes": []})

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"processes": []}
        return read_json(self.path)

    def register(
        self,
        *,
        task_id: str,
        phase: str,
        pid: int,
        command: list[str],
        kind: str = "codex_cli",
    ) -> None:
        with locked(self.path):
            data = self.read()
            processes = [item for item in data.get("processes", []) if item.get("pid") != pid]
            processes.append(
                {
                    "task_id": task_id,
                    "phase": phase,
                    "pid": pid,
                    "kind": kind,
                    "command": command[:8],
                    "started_at": now_iso(),
                }
            )
            data["processes"] = processes
            write_json(self.path, data)

    def unregister(self, pid: int) -> None:
        with locked(self.path):
            data = self.read()
            data["processes"] = [item for item in data.get("processes", []) if item.get("pid") != pid]
            write_json(self.path, data)

    def active(self, task_id: str | None = None) -> list[dict[str, Any]]:
        items = []
        stale_pids: list[int] = []
        for item in self.read().get("processes", []):
            if task_id and item.get("task_id") != task_id:
                continue
            pid = item.get("pid")
            if not isinstance(pid, int) or not is_process_alive(pid):
                continue
            if _matches_registered_process(item):
                items.append(item)
            else:
                # The PID is alive but its command line no longer matches what
                # we registered, so the OS has recycled it for an unrelated
                # process. Drop it instead of risking a signal to a stranger.
                stale_pids.append(pid)
        for pid in stale_pids:
            self.unregister(pid)
        return items

    def interrupt(self, task_id: str | None = None, hard: bool = False) -> list[dict[str, Any]]:
        interrupted = []
        for item in self.active(task_id):
            pid = int(item["pid"])
            try:
                os.kill(pid, signal.SIGINT)
                if hard:
                    os.kill(pid, signal.SIGTERM)
                interrupted.append(item)
            except OSError:
                self.unregister(pid)
        return interrupted


def is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _matches_registered_process(item: dict[str, Any]) -> bool:
    """Guard against PID reuse: confirm the live process at this PID is still
    the one we registered before we trust it enough to signal it."""
    pid = item.get("pid")
    if not isinstance(pid, int):
        return False
    recorded_command = item.get("command") or []
    if not recorded_command:
        return True
    current_command = _process_command_line(pid)
    if current_command is None:
        return False
    return str(recorded_command[0]) in current_command


def _process_command_line(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    return output or None


def now_iso() -> str:
    return datetime.now(tz=KST).isoformat(timespec="seconds")
