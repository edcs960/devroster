"""Portable bounded NDJSON pipes, shared deadlines and owned process-tree cleanup."""

import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time

# The Windows bootstrap below also runs directly, before any package imports.
if __name__ == "__main__":
    # The parent assigns this gated bootstrap to its Job before releasing it.
    # Descendants inherit the Job, even if their immediate parent exits first.
    if os.read(sys.stdin.fileno(), 1) != b"\n":
        sys.exit(1)
    sys.exit(subprocess.Popen(sys.argv[1:]).wait())

from agent_dev.adapters.bounded_process import _exited_unreaped, _signal_owned_group, _wait_options
from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.mcp_protocol import MAX_BYTES, decode_object


class TransportTimeout(DomainError):
    def __init__(self):
        super().__init__(
            ErrorCode.EXTERNAL_TRANSIENT, "MCP 응답/쓰기 전체 실행 시간이 초과되었습니다."
        )


class WindowsJob:
    """A kernel Job owns descendants; taskkill cannot find an already exited root."""

    def __init__(self, process):
        if os.name != "nt":
            raise RuntimeError("Windows Job requires Windows")
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel, self.handle = kernel, kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, no breakaway.
        if (
            not self.handle
            or not kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            )
            or not kernel.AssignProcessToJobObject(self.handle, int(process._handle))
        ):
            if self.handle:
                kernel.CloseHandle(self.handle)
            process.kill()
            process.wait(timeout=1)
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE, "MCP 프로세스 트리 소유권 확보에 실패했습니다."
            )

    def close(self):
        if os.name != "nt":
            return
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class StdioTransport:
    def __init__(self, argv, *, cwd, env, deadline, secrets=()):
        if time.monotonic() >= deadline:
            raise TransportTimeout()
        self.stop = threading.Event()
        self.incoming = queue.Queue(maxsize=16)
        self.outgoing = queue.Queue(maxsize=1)
        self.buffer = bytearray()
        self.last_frame_bytes = 0
        self.secrets = tuple(secrets)
        self._diagnostics = ""
        self.closed = False
        self.job = None
        command = argv if os.name != "nt" else [sys.executable, __file__, *argv]
        if os.name != "nt":
            _wait_options()
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=os.name != "nt",
        )
        try:
            if os.name == "nt":
                self.job = WindowsJob(self.process)
            self.threads = [
                threading.Thread(target=target, name="mcp-" + name, daemon=True)
                for name, target in [
                    ("stdout", self._reader),
                    ("stderr", self._stderr),
                    ("writer", self._writer),
                ]
            ]
            for thread in self.threads:
                thread.start()
            if os.name == "nt":
                self.write(b"\n", deadline)
            if time.monotonic() >= deadline:
                raise TransportTimeout()
        except BaseException:
            self.close()
            raise

    @property
    def diagnostics(self):
        return self._diagnostics

    def _put(self, value):
        while not self.stop.is_set():
            try:
                self.incoming.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def _reader(self):
        assert self.process.stdout is not None
        try:
            while not self.stop.is_set():
                chunk = os.read(self.process.stdout.fileno(), 65536)
                self._put(chunk)
                if not chunk:
                    break
        except OSError:
            self._put(b"")

    def _stderr(self):
        assert self.process.stderr is not None
        pending, discarded = b"", False
        try:
            while not self.stop.is_set():
                chunk = os.read(self.process.stderr.fileno(), 4096)
                if not chunk:
                    if pending and not discarded:
                        self._diagnostic(pending)
                    break
                for part in chunk.splitlines(keepends=True):
                    pending += part
                    if len(pending) > 8192:
                        pending, discarded = b"", True
                    if part.endswith((b"\n", b"\r")):
                        if not discarded:
                            self._diagnostic(pending)
                        pending, discarded = b"", False
        except OSError:
            pass

    def _diagnostic(self, raw):
        text = redact(raw.decode("utf-8", errors="replace"), self.secrets)
        text = re.sub(
            r"(?i)(authorization|password|token|secret|api[_-]?key)\s*[:=]\s*\S+", r"\1=****", text
        )
        self._diagnostics = (self._diagnostics + text)[-4096:]

    def _writer(self):
        assert self.process.stdin is not None
        while not self.stop.is_set():
            try:
                data, done, errors = self.outgoing.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                view = memoryview(data)
                while view and not self.stop.is_set():
                    count = os.write(self.process.stdin.fileno(), view)
                    view = view[count:]
            except OSError:
                errors.append(True)
            finally:
                done.set()

    def write(self, data, deadline):
        if len(data) > MAX_BYTES + 1:
            raise DomainError(ErrorCode.INVALID_INPUT, "MCP stdio 요청 크기 한도를 초과했습니다.")
        done, errors = threading.Event(), []
        remaining = deadline - time.monotonic()
        if self.closed or remaining <= 0:
            raise TransportTimeout()
        while not self.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout()
            try:
                self.outgoing.put((data, done, errors), timeout=min(remaining, 0.05))
                break
            except queue.Full:
                continue
        while not done.is_set() and not self.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout()
            done.wait(min(remaining, 0.05))
        if errors or self.closed:
            raise DomainError(ErrorCode.EXTERNAL_TRANSIENT, "MCP 프로세스 쓰기가 종료되었습니다.")

    def read(self, deadline):
        while True:
            if b"\n" in self.buffer:
                raw, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                if len(raw) > MAX_BYTES:
                    raise DomainError(
                        ErrorCode.INVALID_INPUT, "MCP stdio 응답 크기 한도를 초과했습니다."
                    )
                self.last_frame_bytes = len(raw)
                return decode_object(raw)
            if len(self.buffer) > MAX_BYTES:
                raise DomainError(
                    ErrorCode.INVALID_INPUT, "MCP stdio 응답 크기 한도를 초과했습니다."
                )
            remaining = deadline - time.monotonic()
            if self.closed or remaining <= 0:
                raise TransportTimeout()
            try:
                chunk = self.incoming.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if not chunk:
                if self.buffer:
                    raise DomainError(
                        ErrorCode.INVALID_INPUT, "MCP NDJSON framing: 마지막 개행이 없습니다."
                    )
                raise DomainError(ErrorCode.EXTERNAL_TRANSIENT, "MCP 프로세스가 종료되었습니다.")
            self.buffer.extend(chunk)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop.set()
        if self.job:
            self.job.close()
        elif os.name != "nt":
            _signal_owned_group(self.process, signal.SIGTERM)
            deadline = time.monotonic() + 0.15
            while not _exited_unreaped(self.process) and time.monotonic() < deadline:
                time.sleep(0.005)
            # Even an exited leader pins the group until both signals finish.
            _signal_owned_group(self.process, signal.SIGKILL)
            from agent_dev.adapters.pipe_ownership import cleanup_pipe

            for pipe in (self.process.stdout, self.process.stderr):
                if pipe is not None:
                    cleanup_pipe(pipe.fileno())
        else:
            self.process.kill()
        self.process.wait(timeout=2)
        for thread in getattr(self, "threads", []):
            thread.join(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe:
                pipe.close()
