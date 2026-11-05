"""Build the DevRoster desktop launcher with PyInstaller.

    python -m pip install -e ".[app]"
    python scripts/build_app.py [--onefile] [--smoke]

Output: dist-app/DevRoster/ (folder build; DevRoster.exe on Windows).
Build on each target OS; PyInstaller does not cross-compile. The launcher
starts the local web workspace and opens the browser; AI CLIs (codex, claude)
must be installed separately and are logged in from the settings screen.
--smoke starts the built launcher without a browser and checks /setup.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist-app"
WORK = ROOT / "build-app"
NAME = "DevRoster"


def build(onefile: bool) -> Path:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        NAME,
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        "--specpath",
        str(WORK),
        "--collect-submodules",
        "agent_dev",
        "--collect-data",
        "agent_dev",
        "--onefile" if onefile else "--onedir",
        str(ROOT / "agent_dev" / "app.py"),
    ]
    subprocess.run(command, check=True, cwd=ROOT)
    suffix = ".exe" if os.name == "nt" else ""
    return DIST / (f"{NAME}{suffix}" if onefile else f"{NAME}/{NAME}{suffix}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def smoke(executable: Path) -> None:
    port = free_port()
    with tempfile.TemporaryDirectory(prefix="devroster-app-") as folder:
        env = dict(os.environ, DEVROSTER_HOME=str(Path(folder) / "home"))
        project = Path(folder) / "project"
        project.mkdir()
        process = subprocess.Popen(
            [str(executable), "--root", str(project), "--port", str(port), "--no-browser"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.time() + 60
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/setup", timeout=2) as page:
                        assert "settings-form" in page.read().decode("utf-8")
                    break
                except OSError:
                    if process.poll() is not None or time.time() > deadline:
                        raise RuntimeError("launcher did not serve /setup:\n" + process.stdout.read())
                    time.sleep(1)
            homes = Path(folder) / "home" / "cli"
            assert (homes / "claude").is_dir() and (homes / "codex").is_dir()
            print(f"smoke ok: {executable} served /setup on port {port}")
        finally:
            process.terminate()
            process.wait(timeout=10)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--onefile", action="store_true", help="Single executable (slower start)")
    parser.add_argument("--smoke", action="store_true", help="Start the build and check /setup")
    args = parser.parse_args(argv)
    if subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"], capture_output=True, check=False
    ).returncode:
        print('PyInstaller가 필요합니다: python -m pip install -e ".[app]"')
        return 1
    executable = build(args.onefile)
    print(f"built: {executable}")
    if args.smoke:
        smoke(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
