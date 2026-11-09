"""CLI supervisor: terminate the entire provider process group on parent/lease loss."""

import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

try:
    from agent_dev.adapters.windows_launch import windows_command
except ImportError:  # started as a script without the package on sys.path
    from windows_launch import windows_command  # type: ignore[no-redef]


def parent_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
    finally:
        kernel.CloseHandle(handle)


def terminate_tree(child):
    if child.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            child.kill()
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--holder", required=True)
    parser.add_argument("--token", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    child = subprocess.Popen(windows_command(command), start_new_session=True)

    def terminate(*unused):
        terminate_tree(child)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        while child.poll() is None:
            valid = False
            try:
                if not parent_alive(args.parent):
                    terminate()
                    break
                connection = sqlite3.connect(
                    Path(args.db).resolve().as_uri() + "?mode=ro", uri=True, timeout=1
                )
                try:
                    row = connection.execute(
                        "SELECT holder,token,expires_at FROM leases WHERE resource=?",
                        (args.resource,),
                    ).fetchone()
                    valid = bool(
                        row
                        and row[0] == args.holder
                        and row[1] == args.token
                        and row[2] > time.time()
                    )
                finally:
                    connection.close()
            except (OSError, sqlite3.Error):
                pass
            if not valid:
                terminate()
                break
            time.sleep(0.2)
        return child.wait()
    finally:
        terminate()


if __name__ == "__main__":
    sys.exit(main())
