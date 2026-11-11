"""Desktop launcher: start the local web workspace and open it in the browser.

The packaged app (exe/.app) runs this. All work then happens in the web UI on
127.0.0.1; AI CLIs always use the app-private homes from ``core.cli_home``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from agent_dev.core.cli_home import app_home, apply_to_process
from agent_dev.domain.errors import DomainError

STATE_FILE = "app.json"
INSTANCE_FILE = "app-instance.json"
DEFAULT_PORT = 8767
# Where AI CLIs usually live; a Finder-launched app only gets /usr/bin:/bin:/usr/sbin:/sbin.
COMMON_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.npm-global/bin", "~/.bun/bin")


def _answers(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/api/app/status", timeout=1.5) as response:
            status = json.loads(response.read().decode("utf-8"))
        return isinstance(status, dict) and "can_quit" in status
    except (OSError, ValueError):
        return False


def running_instance() -> str | None:
    """Base URL of a DevRoster app already running on this computer, if it answers."""
    ports = []
    try:
        ports.append(int(json.loads((app_home() / INSTANCE_FILE).read_text(encoding="utf-8"))["port"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # One that is still starting may not have written its file yet; it listens here first.
    ports.append(DEFAULT_PORT)
    for port in dict.fromkeys(ports):
        base = f"http://127.0.0.1:{port}"
        if _answers(base):
            return base
    return None


def record_instance(port: int) -> None:
    path = app_home() / INSTANCE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")


def clear_instance() -> None:
    path = app_home() / INSTANCE_FILE
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def packaged_path(current: str, shell_path: str | None) -> str:
    """PATH for a packaged app: the login shell's PATH, else the usual CLI folders added."""
    parts = [p for p in (shell_path or "").split(os.pathsep) if p] + current.split(os.pathsep)
    if not shell_path:
        parts += [os.path.expanduser(d) for d in COMMON_BIN_DIRS]
    return os.pathsep.join(dict.fromkeys(p for p in parts if p))


def prepare_packaged_app() -> None:
    """A double-clicked app has no terminal: log to a file and find the user's AI CLIs."""
    if not getattr(sys, "frozen", False):
        return
    if sys.stdout is None or sys.stderr is None:
        logs = app_home() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stream = open(logs / "app.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the process
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    if sys.platform == "darwin":
        import subprocess

        shell = os.environ.get("SHELL") or "/bin/zsh"
        try:
            found = subprocess.run(
                [shell, "-l", "-c", 'printf %s "$PATH"'],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=5,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            found = ""
        os.environ["PATH"] = packaged_path(os.environ.get("PATH", ""), found or None)


def remembered_root() -> Path | None:
    try:
        value = json.loads((app_home() / STATE_FILE).read_text(encoding="utf-8"))
        root = Path(value["root"]).expanduser()
        return root if root.is_dir() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remember_root(root: Path) -> None:
    from agent_dev.adapters.workspace_registry import register_root

    register_root(root, remember=True)


def resolve_root(root: Path | None) -> Path:
    if root:
        return Path(root).expanduser().resolve()
    remembered = remembered_root()
    if remembered:
        from agent_dev.adapters.workspace_registry import configuration_for
        return configuration_for(remembered)
    # A double-clicked app may start in "/" (macOS) or its install folder.
    return Path.home()


def start_path(root: Path) -> str:
    from agent_dev.ui.studio_server import start_path as page

    return page(root)


def upgrade_state_database(root: Path) -> bool:
    """Migrate an existing workspace database before any page opens it read-only.

    Pages read with read-only connections, which never run migrations, so a workspace created by
    an older release would otherwise keep its old schema until the first write. A newer schema or
    a locked file is left alone here; the pages report those cases themselves.
    """
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.adapters.storage_registry import state_db_path
    from agent_dev.adapters.workspace import configuration_root

    try:
        path = state_db_path(configuration_root(Path(root)))
        if not path.is_file():
            return False
        Database(path)
        return True
    except Exception:  # noqa: BLE001 - start-up must not fail because of one workspace
        return False


def make_server(root: Path, port: int):
    from agent_dev.ui.studio_server import make_server as studio_server

    upgrade_state_database(root)
    try:
        return studio_server(root, port)
    except OSError:
        return studio_server(root, 0)  # Port busy: let the OS pick a free one.


def main(argv=None) -> None:
    from agent_dev.core.stdio import use_utf8_stdio

    use_utf8_stdio()  # Korean help and status lines on Windows pipes.
    from agent_dev.adapters.workspace_trust import ensure_seeded

    ensure_seeded()  # devroster-app starts here directly; seed before registering --root.
    parser = argparse.ArgumentParser(prog="devroster app", description=(__doc__ or "DevRoster app").splitlines()[0])
    parser.add_argument("--root", type=Path, help="Project folder (default: last used)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    prepare_packaged_app()
    apply_to_process()
    root = resolve_root(args.root)
    if args.root and root.is_dir():
        remember_root(root)
    existing = None if args.root else running_instance()
    if existing:
        # Opening the app again shows the running one instead of starting a second copy.
        print(f"이미 실행 중인 DevRoster를 엽니다: {existing}", flush=True)
        if not args.no_browser:
            webbrowser.open(existing + start_path(root))
        return
    server = make_server(root, args.port)
    server.studio.quit = server.shutdown  # The screen's 앱 종료 stops serve_forever below.
    record_instance(server.server_port)
    try:
        server.studio.setup.features.messenger.autostart()
    except DomainError as error:
        print(f"슬랙 리스너를 시작하지 못했습니다: {error}", flush=True)
    url = f"http://127.0.0.1:{server.server_port}{start_path(root)}"
    print(f"DevRoster: {url}", flush=True)
    print(f"프로젝트 폴더: {root}", flush=True)
    print("종료: 화면 메뉴의 '앱 종료', 이 창을 닫기, 또는 Ctrl+C", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        clear_instance()
        server.studio.close()
        server.server_close()


if __name__ == "__main__":
    main()
