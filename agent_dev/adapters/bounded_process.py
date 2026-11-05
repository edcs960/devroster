"""Subprocess output with a fixed memory budget and one wall-clock deadline."""

import ctypes
import errno
import io
import logging
import os
import selectors
import signal
import struct
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import cast

from agent_dev.adapters.pipe_ownership import cleanup_pipe, spawn
from agent_dev.domain.errors import DomainError, ErrorCode

logger = logging.getLogger(__name__)


# <sys/wait.h> on macOS: P_PID, WEXITED | WNOHANG | WNOWAIT. `siginfo_t.si_pid` is at offset 12.
_DARWIN_P_PID = 1
_DARWIN_WAIT_OPTIONS = 0x4 | 0x1 | 0x20
_SIGINFO_SIZE = 128


def _libc_waitid():
    """os.waitid exists on macOS only from Python 3.13; older versions call libc directly."""
    if hasattr(os, "waitid") or sys.platform != "darwin":
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).waitid
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


_LIBC_WAITID = _libc_waitid()


def _wait_options():
    if hasattr(os, "waitid") and hasattr(os, "P_PID"):
        return os.WEXITED | os.WNOHANG | os.WNOWAIT
    if _LIBC_WAITID is not None:
        return _DARWIN_WAIT_OPTIONS
    raise DomainError(
        ErrorCode.CAPABILITY_UNAVAILABLE,
        "회수 전 프로세스 그룹 소유권을 확인할 수 없습니다.",
    )


def _exited_pid(pid):
    """PID of an exited but still unreaped child (0 while it runs); ChildProcessError once reaped.

    WNOWAIT keeps the zombie, so the PID and its group cannot be reused while we hold it.
    """
    options = _wait_options()
    if hasattr(os, "waitid"):
        result = os.waitid(os.P_PID, pid, options)
        return 0 if result is None else result.si_pid
    assert _LIBC_WAITID is not None
    info = ctypes.create_string_buffer(_SIGINFO_SIZE)
    while _LIBC_WAITID(_DARWIN_P_PID, pid, info, options) != 0:
        code = ctypes.get_errno()
        if code == errno.EINTR:
            continue
        if code == errno.ECHILD:
            raise ChildProcessError(code, os.strerror(code))
        raise OSError(code, os.strerror(code))
    return struct.unpack_from("=i", info.raw, 12)[0]


def _exited_unreaped(process):
    """Observe exit without releasing the child PID/group identity."""
    with process._waitpid_lock:
        if process.returncode is not None:
            return True
        try:
            return _exited_pid(process.pid) != 0
        except ChildProcessError:
            return True


def _signal_owned_group(process, sig):
    # Serialize with Popen poll/wait; never signal a reaped, reusable PID.
    with process._waitpid_lock:
        if process.returncode is not None:
            return
        try:
            exited = _exited_pid(process.pid)
        except ChildProcessError:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS rejects a zombie-only group. Keep its original exit status.
            if exited == 0:
                # send_signal's internal poll cannot reap while we hold this lock.
                process.send_signal(sig)


@dataclass(frozen=True)
class ProcessOutput:
    returncode: int
    output: bytes
    truncated: bool
    overflow: bool


def _terminate(process):
    if os.name != "nt":
        _signal_owned_group(process, signal.SIGKILL)
    elif os.name == "nt":
        from agent_dev.adapters.process_guard import terminate_tree

        terminate_tree(process)
    process.wait()


def run_bounded(command, *, cwd, env, timeout, limit, tail=None, stderr=subprocess.STDOUT):
    """Keep a prefix, or a tail, and terminate on deadline or total byte limit."""
    if os.name != "nt":
        _wait_options()  # Fail before spawning if safe group pinning is unavailable.
    deadline = time.monotonic() + timeout
    process, job = spawn(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=stderr, start_new_session=os.name != "nt",
    )
    output = bytearray()
    total = 0
    overflow = threading.Event()
    drained = threading.Event()
    stop = threading.Event()
    errors = []

    def collect():
        nonlocal total
        try:
            assert process.stdout is not None
            stream = cast(io.BufferedReader, process.stdout)
            fd = stream.fileno()
            with selectors.DefaultSelector() if os.name != "nt" else nullcontext() as selector:
                if selector is not None:
                    os.set_blocking(fd, False)
                    selector.register(fd, selectors.EVENT_READ)
                while not stop.is_set():
                    size = min(65536, limit + 1 - total)
                    if selector is not None:
                        if not selector.select(timeout=0.05):
                            continue
                        try:
                            chunk = os.read(fd, size)
                        except BlockingIOError:
                            continue
                    else:
                        chunk = stream.read1(size)
                    if not chunk:
                        break
                    total += len(chunk)
                    output.extend(chunk)
                    if tail is not None:
                        del output[:-tail]
                    if total > limit:
                        overflow.set()
                        break
        except OSError as exc:
            errors.append(exc)
        finally:
            drained.set()

    reader = threading.Thread(target=collect, daemon=True)
    try:
        reader.start()
        while True:
            if overflow.is_set():
                _terminate(process)
                break
            if drained.is_set() and (
                _exited_unreaped(process) if os.name != "nt" else process.poll() is not None
            ):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            reader.join(timeout=min(0.02, remaining))
            if drained.is_set():
                time.sleep(min(0.01, remaining))
    finally:
        try:
            _terminate(process)
        finally:
            if job is not None:
                job.close()
            stop.set()
            if reader.ident is not None:
                reader.join(timeout=1)
            if process.stdout is not None:
                has_primary_error = sys.exc_info()[0] is not None
                try:
                    if os.name != "nt":
                        cleanup_pipe(process.stdout.fileno())
                except DomainError:
                    if not has_primary_error:
                        raise
                    logger.warning(
                        "Process cleanup could not be proven; preserving the original error."
                    )
                finally:
                    process.stdout.close()
    if errors:
        raise errors[0]
    return ProcessOutput(
        process.returncode, bytes(output[:limit]), total > (tail or limit), overflow.is_set()
    )
