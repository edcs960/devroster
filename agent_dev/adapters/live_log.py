"""Per-attempt live log of what an AI CLI is doing, read by the operator console.

Lines come from domain.live_log.summarize (no model reasoning), secrets are masked, the file
is capped, and logs older than RETENTION_DAYS in the same task folder are removed. The file
sits next to the attempt baselines (<task folder>/.attempts), outside every repository and
outside backups, and goes away with `devroster worktree prune`.
"""

import threading
import time
from pathlib import Path

from agent_dev.domain.content import redact
from agent_dev.domain.live_log import summarize

LIMIT_BYTES = 2_000_000
RETENTION_DAYS = 30
READ_CHUNK = 65_536


def log_path(bindings, attempt_id):
    from agent_dev.adapters.role_execution import _attempts_dir

    return _attempts_dir(bindings) / f"{attempt_id}.log"


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
        cutoff = self.clock() - RETENTION_DAYS * 86400
        for old in self.path.parent.glob("*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

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
