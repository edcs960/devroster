"""Desktop launcher: start the local web workspace and open it in the browser.

The packaged app (exe/.app) runs this. All work then happens in the web UI on
127.0.0.1; AI CLIs always use the app-private homes from ``core.cli_home``.
"""

from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from pathlib import Path

from agent_dev.core.cli_home import app_home, apply_to_process
from agent_dev.domain.errors import DomainError

STATE_FILE = "app.json"
DEFAULT_PORT = 8767


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
    from agent_dev.adapters.project_setup import ProjectSetup

    try:
        configured = ProjectSetup(root).read()["revision"] > 0
    except Exception:  # noqa: BLE001 - an unreadable workspace still opens the setup screen
        configured = False
    return "/agent" if configured else "/setup"


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
    parser = argparse.ArgumentParser(prog="devroster app", description=(__doc__ or "DevRoster app").splitlines()[0])
    parser.add_argument("--root", type=Path, help="Project folder (default: last used)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    apply_to_process()
    root = resolve_root(args.root)
    if args.root and root.is_dir():
        remember_root(root)
    server = make_server(root, args.port)
    try:
        server.studio.setup.features.messenger.autostart()
    except DomainError as error:
        print(f"슬랙 리스너를 시작하지 못했습니다: {error}", flush=True)
    url = f"http://127.0.0.1:{server.server_port}{start_path(root)}"
    print(f"DevRoster: {url}", flush=True)
    print(f"프로젝트 폴더: {root}", flush=True)
    print("종료: 이 창을 닫거나 Ctrl+C", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.studio.close()
        server.server_close()


if __name__ == "__main__":
    main()
