"""Run gates under a parent/lease supervisor with bounded output and deadlines."""
import os
import signal
import subprocess
import sys
import threading
import time
from io import BufferedReader
from pathlib import Path
from typing import cast


def run_gate(args, cwd, environment, timeout, db_path, lease):
    command = [sys.executable, str(Path(__file__).with_name("process_guard.py")),
               "--parent", str(os.getpid()), "--db", str(db_path),
               "--resource", lease["resource"], "--holder", lease["holder"],
               "--token", str(lease["token"]), "--", *args]
    process = subprocess.Popen(command, cwd=cwd, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    tail = bytearray()
    overflow = threading.Event()

    def collect():
        total = 0
        assert process.stdout is not None
        while chunk := cast(BufferedReader, process.stdout).read1(65536):
            total += len(chunk)
            tail.extend(chunk)
            del tail[:-12000]
            if total > 2_000_000:
                overflow.set()

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if time.monotonic() >= deadline or overflow.is_set():
                raise subprocess.TimeoutExpired(args, timeout)
            time.sleep(.05)
        reader.join(timeout=1)
        if overflow.is_set():
            raise subprocess.TimeoutExpired(args, timeout)
        return process.returncode, bytes(tail).decode("utf-8", errors="replace")
    finally:
        if process.poll() is None:
            if os.name == "nt":
                from agent_dev.adapters.process_guard import terminate_tree
                terminate_tree(process)
            else:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        reader.join(timeout=1)
        if process.stdout:
            process.stdout.close()
