"""macOS before Python 3.13 has no os.waitid; bounded processes fall back to libc waitid."""

import os
import subprocess
import sys
import time

import pytest

from agent_dev.adapters import bounded_process

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="libc waitid fallback is macOS-only")


@pytest.fixture
def libc_only(monkeypatch):
    """Simulate Python 3.11/3.12 on macOS even when the interpreter has os.waitid."""
    monkeypatch.delattr(os, "waitid", raising=False)
    function = bounded_process._libc_waitid()
    assert function is not None
    monkeypatch.setattr(bounded_process, "_LIBC_WAITID", function)
    return function


def test_exited_pid_observes_exit_without_reaping(libc_only):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"])
    try:
        assert bounded_process._exited_pid(process.pid) == 0
        deadline = time.monotonic() + 10
        while bounded_process._exited_pid(process.pid) == 0:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        # Still a zombie: the PID cannot be reused until we reap it.
        assert bounded_process._exited_pid(process.pid) == process.pid
        assert process.returncode is None
    finally:
        process.wait()
    with pytest.raises(ChildProcessError):
        bounded_process._exited_pid(process.pid)


def test_run_bounded_works_with_libc_fallback(libc_only, tmp_path):
    result = bounded_process.run_bounded(
        [sys.executable, "-c", "print('ok')"], cwd=tmp_path, env=None, timeout=10, limit=1000
    )
    assert result.returncode == 0
    assert result.output.strip() == b"ok"


def test_deadline_still_kills_with_libc_fallback(libc_only, tmp_path):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        bounded_process.run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env=None,
            timeout=0.5,
            limit=1000,
        )
    assert time.monotonic() - started < 10
