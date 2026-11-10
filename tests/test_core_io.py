"""Exercise native file locking across processes on every supported OS."""

import subprocess
import sys

import pytest

from agent_dev.core.io import locked, read_json, write_json


def test_concurrent_json_updates_do_not_lose_writes(tmp_path):
    store = tmp_path / "counter.json"
    write_json(store, {"count": 0})
    script = """
import sys
from pathlib import Path
from agent_dev.core.io import locked, read_json, write_json
store = Path(sys.argv[1])
for _ in range(30):
    with locked(store):
        data = read_json(store)
        data['count'] += 1
        write_json(store, data)
"""
    processes = []
    try:
        with locked(store):
            for _ in range(4):
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-c", script, str(store)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
        assert read_json(store) == {"count": 120}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_lock_released_on_exception(tmp_path):
    store = tmp_path / "counter.json"
    with pytest.raises(RuntimeError), locked(store):
        raise RuntimeError("operation failed")
    script = """
import sys
from pathlib import Path
from agent_dev.core.io import locked, write_json
store = Path(sys.argv[1])
with locked(store):
    write_json(store, {'recovered': True})
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store)],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert read_json(store) == {"recovered": True}


def test_io_module_import_does_not_require_unix_locking():
    script = """
import builtins
original_import = builtins.__import__
def import_without_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ModuleNotFoundError('fcntl unavailable')
    return original_import(name, *args, **kwargs)
builtins.__import__ = import_without_fcntl
from agent_dev.core.io import locked, read_json, write_json
assert callable(locked) and callable(read_json) and callable(write_json)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
