"""Per-attempt live log of what an AI CLI is doing, read by the operator console.

Lines come from domain.live_log.summarize (no model reasoning), secrets are masked, the file
is capped, and logs older than RETENTION_DAYS are removed. While the task runs the file sits
next to the attempt baselines (<task folder>/.attempts), outside every repository and outside
backups. When the task folder is removed (completion, `devroster worktree prune`) the logs
move to <worktree root>/.logs/<task> and stay there for RETENTION_DAYS more.
"""

import contextlib
import os
import threading
import time
from pathlib import Path

from agent_dev.domain.content import redact
from agent_dev.domain.live_log import summarize

LIMIT_BYTES = 2_000_000
RETENTION_DAYS = 30
READ_CHUNK = 65_536
ARCHIVE = ".logs"


def log_path(bindings, attempt_id):
    """Where a running attempt writes."""
    from agent_dev.adapters.role_execution import _attempts_dir

    return _attempts_dir(bindings) / f"{attempt_id}.log"


def archive_dir(attempts_dir):
    """<worktree root>/.logs/<task>: outside the task folder, still outside repositories and backups."""
    task_folder = Path(attempts_dir).parent
    return task_folder.parent / ARCHIVE / task_folder.name


def find(bindings, attempt_id):
    """The attempt's log to read: in the task folder while that exists, in the archive afterwards."""
    live = log_path(bindings, attempt_id)
    return live if live.is_file() else archive_dir(live.parent) / live.name


def _drop_old(logs, cutoff):
    for old in logs:
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def sweep_archive(root, clock=time.time):
    """Remove archived logs older than RETENTION_DAYS and the task folders they leave empty."""
    root = Path(root)
    if not root.is_dir():
        return
    _drop_old(root.glob("*/*.log"), clock() - RETENTION_DAYS * 86400)
    for folder in root.iterdir():
        with contextlib.suppress(OSError):
            folder.rmdir()  # Only succeeds when empty.


def archive(attempts_dir, clock=time.time):
    """Move a task's logs out of a folder that is about to be removed; returns how many moved.

    Retention restarts here, so a completed task's logs stay readable for RETENTION_DAYS
    after completion. A log that cannot be moved is left to go with the folder.
    """
    target = archive_dir(attempts_dir)
    now = clock()
    moved = 0
    for log in sorted(Path(attempts_dir).glob("*.log")):
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.replace(log, target / log.name)
            os.utime(target / log.name, (now, now))
            moved += 1
        except OSError:
            pass
    sweep_archive(target.parent, clock)
    return moved


class LiveLog:
    """Thread-safe line sink: ``write(stream, raw_line)`` is called by the output readers."""

    def __init__(self, path, provider, secrets=(), clock=time.time):
        self.path, self.provider, self.secrets, self.clock = Path(path), provider, tuple(secrets), clock
        self.lock = threading.Lock()
        self.size = 0
        self.full = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sweep()
        self.path.write_text("", encoding="utf-8")

    def _sweep(self):
        _drop_old(self.path.parent.glob("*.log"), self.clock() - RETENTION_DAYS * 86400)
        sweep_archive(archive_dir(self.path.parent).parent, self.clock)

    def write(self, stream, raw_line):
        line = summarize(self.provider, stream, raw_line)
        if line is None:
            return
        text = redact(line, self.secrets)
        stamp = time.strftime("%H:%M:%S", time.localtime(self.clock()))
        entry = "".join(f"{stamp} {part}\n" for part in str(text).splitlines())
        with self.lock:
            if self.full:
                return
            data = entry.encode("utf-8")
            if self.size + len(data) > LIMIT_BYTES:
                data = f"{stamp} (로그가 {LIMIT_BYTES // 1_000_000}MB에 도달해 이후 내용은 기록하지 않습니다)\n".encode()
                self.full = True
            with self.path.open("ab") as handle:
                handle.write(data)
            self.size += len(data)


def read(path, offset=0, limit=READ_CHUNK):
    """(text, next offset) from ``offset``; never splits a UTF-8 character."""
    path = Path(path)
    if not path.is_file():
        return "", 0
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        offset = max(0, min(int(offset), size))
        handle.seek(offset)
        data = handle.read(limit)
    end = len(data)
    if offset + end < size:
        cut = data.rfind(b"\n")
        end = cut + 1 if cut >= 0 else end
    return data[:end].decode("utf-8", errors="replace"), offset + end
