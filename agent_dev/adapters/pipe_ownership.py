"""Own lingering stdout writers by pipe identity, never by a bare discovered PID."""

import ctypes
import errno
import os
import signal
import struct
import subprocess
import sys
import time

# Windows starts a gated supervisor; no target executes before Job assignment.
if __name__ == '__main__':
    if os.read(sys.stdin.fileno(), 1) != b'\n':
        sys.exit(1)
    sys.exit(subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL).wait())

from pathlib import Path
from typing import NoReturn

from agent_dev.domain.errors import DomainError, ErrorCode


def unavailable() -> NoReturn:
    raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE,
                      '출력 pipe의 프로세스 소유권을 안전하게 확인할 수 없습니다.')


def spawn(command, **kwargs):
    if os.name != 'nt':
        return subprocess.Popen(command, **kwargs), None
    from agent_dev.adapters.stdio_transport import WindowsJob

    process = subprocess.Popen([sys.executable, __file__, *command],
                               **dict(kwargs, stdin=subprocess.PIPE))
    job = None
    try:
        job = WindowsJob(process)
        process.stdin.write(b'\n')
        process.stdin.flush()
        process.stdin.close()
        return process, job
    except BaseException:
        if job is not None:
            job.close()
        process.kill()
        process.wait()
        process.stdin.close()
        process.stdout.close()
        raise


def pipe_still_open(fd):
    """Discard residual bytes after the reader stopped; no expensive scan on EOF."""
    os.set_blocking(fd, False)
    for _ in range(16):
        try:
            if not os.read(fd, 65536):
                return False
        except BlockingIOError:
            return True
    return True


def _mac_library():
    try:
        lib = ctypes.CDLL('/usr/lib/libproc.dylib')
        lib.proc_signal_with_audittoken.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.proc_signal_with_audittoken.restype = ctypes.c_int
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_void_p, ctypes.c_int]
        lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.c_int]
        return lib
    except (OSError, AttributeError):
        unavailable()


def _mac_info(lib, pid, flavor, size):
    buffer = ctypes.create_string_buffer(size)
    count = lib.proc_pidinfo(pid, flavor, 0, buffer, size)
    return buffer.raw[:count] if count > 0 else b''


def _mac_identity(lib, pid):
    raw = _mac_info(lib, pid, 17, 56)
    if len(raw) != 56:
        return None
    return struct.unpack_from('=Q', raw, 16)[0], struct.unpack_from('=i', raw, 32)[0]


def _mac_pipe(lib, pid, fd):
    buffer = ctypes.create_string_buffer(184)
    if lib.proc_pidfdinfo(pid, fd, 6, buffer, 184) != 184:
        return None
    flags = struct.unpack_from('=I', buffer.raw)[0]
    handle, peer = struct.unpack_from('=QQ', buffer.raw, 160)
    return flags, handle, peer


def _mac_cleanup(fd):
    lib = _mac_library()
    own = _mac_pipe(lib, os.getpid(), fd)
    if own is None:
        unavailable()
    if not own[2]:
        return
    target_handle = own[2]
    # Kernel pointer identity remains pinned by our open read endpoint.
    size = lib.proc_listpids(4, os.getuid(), None, 0)
    if size <= 0:
        unavailable()
    pids = (ctypes.c_int * (size // 4 + 64))()
    count = lib.proc_listpids(4, os.getuid(), pids, ctypes.sizeof(pids))
    if count <= 0:
        unavailable()
    for pid in pids[:count // 4]:
        if pid <= 0 or pid == os.getpid():
            continue
        identity = _mac_identity(lib, pid)
        bsd = _mac_info(lib, pid, 3, 136)
        if identity is None or len(bsd) != 136:
            continue
        values = struct.unpack_from('=12I', bsd)
        if values[3] != pid or values[5] != os.getuid():
            continue
        descriptors = _mac_info(lib, pid, 1, 8 * 8192)
        matched = False
        for offset in range(0, len(descriptors) - 7, 8):
            other_fd, kind = struct.unpack_from('=iI', descriptors, offset)
            if kind != 6:
                continue
            pipe = _mac_pipe(lib, pid, other_fd)
            if pipe is not None and pipe[0] & 2 and pipe[1] == target_handle:
                matched = True
                break
        if not matched or _mac_identity(lib, pid) != identity:
            continue
        # Signal syscall validates PID version atomically, blocking PID reuse.
        token = (ctypes.c_uint32 * 8)(0, values[5], values[6], values[7], values[8], pid, 0, identity[1])
        result = lib.proc_signal_with_audittoken(ctypes.byref(token), signal.SIGKILL)
        if result not in (0, errno.ESRCH):
            unavailable()


def _linux_cleanup(fd):
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        unavailable()
    wanted = f'pipe:[{os.fstat(fd).st_ino}]'
    for candidate in Path('/proc').iterdir():
        if not candidate.name.isdigit() or int(candidate.name) == os.getpid():
            continue
        handle = None
        try:
            handle = os.pidfd_open(int(candidate.name))
            if candidate.stat().st_uid != os.getuid():
                continue
            for entry in (candidate / 'fd').iterdir():
                if os.readlink(entry) != wanted:
                    continue
                flags = next(line.split()[1] for line in (candidate / 'fdinfo' / entry.name).read_text(encoding="utf-8").splitlines()
                             if line.startswith('flags:'))
                if int(flags, 8) & os.O_ACCMODE == os.O_RDONLY:
                    continue
                signal.pidfd_send_signal(handle, signal.SIGKILL)
                break
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        finally:
            if handle is not None:
                os.close(handle)


def cleanup_pipe(fd):
    deadline = time.monotonic() + 0.5
    while pipe_still_open(fd):
        if sys.platform == 'darwin':
            _mac_cleanup(fd)
        elif sys.platform.startswith('linux'):
            _linux_cleanup(fd)
        else:
            unavailable()
        if time.monotonic() >= deadline:
            if pipe_still_open(fd):
                unavailable()
            return
        time.sleep(.01)
