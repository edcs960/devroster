"""Coordinate a web backup's writer transaction with its fenced lease heartbeat."""

import contextlib
import threading
import time

from agent_dev.domain.errors import DomainError, ErrorCode


class WebBackupCapture:
    """Only active, progressing capture may replace the keeper's separate DB write.

    The capture thread renews through its canonical writer connection. The keeper
    still watches the parent and enforces a 30-second maximum without progress.
    """

    def __init__(self, safety, lease, shared_progress=None):
        self.safety, self.lease = safety, lease
        self.mutex = threading.Lock()
        self.active = False
        self.progress_at = 0.0
        self.shared_progress = shared_progress

    def progress(self):
        if time.monotonic() - self.progress_at > 30:
            raise DomainError(ErrorCode.LEASE_CONFLICT, "Backup capture stalled without progress.")
        self.progress_at = time.monotonic()
        if self.shared_progress is not None:
            self.shared_progress.value = self.progress_at

    @contextlib.contextmanager
    def hold(self):
        with self.mutex:
            # Refresh and assert ownership before acquiring either SQLite writer lock.
            self.safety.heartbeat(self.lease, ttl=60)
            self.progress_at = time.monotonic()
            self.active = True
            if self.shared_progress is not None:
                self.shared_progress.value = self.progress_at
            try:
                yield self.progress
            finally:
                self.active = False
                if self.shared_progress is not None:
                    self.shared_progress.value = 0

    def heartbeat(self):
        if self.active:
            if time.monotonic() - self.progress_at > 30:
                raise DomainError(
                    ErrorCode.LEASE_CONFLICT, "Backup capture stalled without progress."
                )
            return
        if self.mutex.acquire(blocking=False):
            try:
                self.safety.heartbeat(self.lease, ttl=60)
            finally:
                self.mutex.release()
