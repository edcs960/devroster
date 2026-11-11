from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    from agent_dev.core.cli_home import apply_to_process
    from agent_dev.core.stdio import use_utf8_stdio

    use_utf8_stdio()
    apply_to_process()
    from agent_dev.adapters.workspace_trust import ensure_seeded

    ensure_seeded()  # Before any command registers a folder (see workspace_trust).
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["app"]:
        from agent_dev.app import main as app_main

        return app_main(args[1:])
    from agent_dev.entrypoints import main as agent_dev_main

    return agent_dev_main(args)


if __name__ == "__main__":
    main()
