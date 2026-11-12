"""Build the DevRoster desktop app with PyInstaller.

    python -m pip install -e ".[app]"
    python scripts/build_app.py --dmg --smoke        # macOS: dist-app/DevRoster-<ver>-macos-<arch>.dmg
    python scripts/build_app.py --onefile --smoke    # Windows: dist-app/DevRoster.exe

Build on each target OS; PyInstaller does not cross-compile (one-click: scripts/build_macos.command, scripts\build_windows.bat).
The app has no console window: it starts the local web workspace, opens the browser and is
stopped from the screen's menu (앱 종료). Opening it again shows the running one. AI CLIs
(codex, claude) are installed separately and logged in from the settings screen.
--smoke starts the built app without a browser, opens a session with a one-time ticket
and checks /setup.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist-app"
WORK = ROOT / "build-app"
NAME = "DevRoster"
ICON_PNG = ROOT / "packaging" / "DevRoster.png"
BUNDLE_ID = "com.devroster.app"


def version() -> str:
    import tomllib

    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def icon_file() -> Path | None:
    """.icns (macOS) or .ico (Windows) made from packaging/DevRoster.png, no extra packages."""
    WORK.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        iconset = WORK / f"{NAME}.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(
                    ["sips", "-z", str(size * scale), str(size * scale), str(ICON_PNG), "--out", str(iconset / name)],
                    check=True, capture_output=True,
                )
        target = WORK / f"{NAME}.icns"
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(target)], check=True)
        return target
    if os.name == "nt":
        # An .ico may hold a PNG image directly (Windows Vista and later).
        data = ICON_PNG.read_bytes()
        import struct

        header = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 22)
        target = WORK / f"{NAME}.ico"
        target.write_bytes(header + data)
        return target
    return None


def build(onefile: bool) -> Path:
    icon = icon_file()
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
        # No console window: the app is stopped from its screen (앱 종료).
        "--windowed",
        "--osx-bundle-identifier",
        BUNDLE_ID,
        *(["--icon", str(icon)] if icon else []),
        str(ROOT / "agent_dev" / "app.py"),
    ]
    subprocess.run(command, check=True, cwd=ROOT)
    if sys.platform == "darwin":
        app = DIST / f"{NAME}.app"
        stamp_version(app)
        return app / "Contents" / "MacOS" / NAME
    suffix = ".exe" if os.name == "nt" else ""
    return DIST / (f"{NAME}{suffix}" if onefile else f"{NAME}/{NAME}{suffix}")


def stamp_version(app: Path) -> None:
    import plistlib

    info = app / "Contents" / "Info.plist"
    value = plistlib.loads(info.read_bytes())
    value.update(
        CFBundleShortVersionString=version(),
        CFBundleVersion=version(),
        CFBundleDisplayName=NAME,
        NSHighResolutionCapable=True,
        LSMinimumSystemVersion="11.0",
    )
    info.write_bytes(plistlib.dumps(value))


def dmg(app: Path) -> Path:
    """A compressed disk image with the app and an Applications shortcut to drag it onto."""
    import platform
    import shutil

    stage = WORK / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    target = DIST / f"{NAME}-{version()}-macos-{platform.machine()}.dmg"
    target.unlink(missing_ok=True)
    subprocess.run(
        ["hdiutil", "create", "-volname", NAME, "-srcfolder", str(stage), "-ov", "-format", "UDZO", str(target)],
        check=True,
    )
    return target


def check_dmg(image: Path) -> None:
    """Mount the image read-only and make sure the app and the Applications link are there."""
    import tempfile as temp

    with temp.TemporaryDirectory(prefix="devroster-dmg-") as mount:
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", mount, str(image)],
            check=True, capture_output=True,
        )
        try:
            assert (Path(mount) / f"{NAME}.app" / "Contents" / "MacOS" / NAME).is_file()
            assert (Path(mount) / "Applications").is_symlink()
        finally:
            subprocess.run(["hdiutil", "detach", mount], check=True, capture_output=True)
    print(f"dmg ok: {image}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def session_address(home: str, base: str, page: str) -> str:
    """A launch address for the app whose app folder is ``home`` (see agent_dev.ui.session)."""
    sys.path.insert(0, str(ROOT))
    from agent_dev.ui import session

    saved = os.environ.get("DEVROSTER_HOME")
    os.environ["DEVROSTER_HOME"] = home
    try:
        return session.launch_url(base, page, session.ticket())
    finally:
        if saved is None:
            del os.environ["DEVROSTER_HOME"]
        else:
            os.environ["DEVROSTER_HOME"] = saved


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
            text=True, encoding="utf-8", errors="replace",
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.time() + 60
            while True:
                try:
                    with urllib.request.urlopen(base + "/api/app/status", timeout=2) as status:
                        assert b"can_quit" in status.read()
                    break
                except OSError:
                    if process.poll() is not None or time.time() > deadline:
                        raise RuntimeError("launcher did not start:\n" + (process.stdout.read() if process.stdout else ""))
                    time.sleep(1)
            # The screens answer only a browser the app opened. Do what a second start of
            # the app does: leave a one-time ticket in the app folder and open /launch.
            try:
                urllib.request.urlopen(base + "/setup", timeout=5)
                raise RuntimeError("the built app served /setup without a session")
            except urllib.error.HTTPError as refused:
                assert refused.code == 403
            browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            with browser.open(session_address(env["DEVROSTER_HOME"], base, "/setup"), timeout=5) as launch:
                assert launch.status == 200
            with browser.open(base + "/setup", timeout=5) as page:
                assert "settings-form" in page.read().decode("utf-8")
            homes = Path(folder) / "home" / "cli"
            assert (homes / "claude").is_dir() and (homes / "codex").is_dir()
            print(f"smoke ok: {executable} served /setup on port {port}")
        finally:
            process.terminate()
            process.wait(timeout=10)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "DevRoster app").splitlines()[0])
    parser.add_argument("--onefile", action="store_true", help="Single executable (slower start)")
    parser.add_argument("--smoke", action="store_true", help="Start the build and check /setup")
    parser.add_argument("--dmg", action="store_true", help="macOS: also make a .dmg disk image")
    args = parser.parse_args(argv)
    if subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"], capture_output=True, check=False
    ).returncode:
        print('PyInstaller가 필요합니다: python -m pip install -e ".[app]"')
        return 1
    if args.dmg and sys.platform != "darwin":
        print("--dmg는 macOS에서만 만들 수 있습니다.")
        return 1
    executable = build(args.onefile and sys.platform != "darwin")
    print(f"built: {executable}")
    if args.smoke:
        smoke(executable)
    if args.dmg:
        image = dmg(executable.parents[2])
        check_dmg(image)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
