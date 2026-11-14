"""Installed entry point for the approved-workflow commands."""

import sys

# Short forms kept from the removed legacy CLI; they map onto task commands.
ALIASES = {"run": "run", "verify": "verify", "resume": "resume", "complete": "complete"}


def web_servers():
    """The screens CLI commands may start; imported only when one is asked for."""

    def lazy(module):
        def serve(root, port):
            from importlib import import_module

            return import_module(module).serve(root, port)

        return serve

    return {
        "studio": lazy("agent_dev.ui.studio_server"),
        "dashboard": lazy("agent_dev.ui.dashboard"),
        "setup": lazy("agent_dev.ui.setup_server"),
    }


def commands():
    """CLI namespaces whose handlers import the command line (see cli_registry)."""

    def messenger(args):
        from agent_dev.adapters.messenger import run_cli

        return run_cli(args)

    return {"messenger": messenger}


def main(argv=None):
    from agent_dev.core.cli_home import apply_to_process
    from agent_dev.core.stdio import use_utf8_stdio

    use_utf8_stdio()
    apply_to_process()
    from agent_dev.adapters.workspace_trust import ensure_seeded

    ensure_seeded()  # Before any command registers a folder (see workspace_trust).
    from agent_dev.adapters.cli_registry import register_commands, register_web_servers
    from agent_dev.adapters.command_line import handles, parser
    from agent_dev.adapters.command_line import main as workflow_main

    register_web_servers(web_servers())
    register_commands(commands())

    arguments = list(sys.argv[1:] if argv is None else argv)
    if handles(arguments):
        return workflow_main(arguments)
    if not arguments or arguments[0] in {"--help", "-h"}:
        parser().print_help()
        return
    if arguments[0] in ALIASES:
        return workflow_main(["task", ALIASES[arguments[0]], *arguments[1:]])
    if arguments[0] == "status":
        return workflow_main(["task", "list", *arguments[1:]])
    # The legacy pipeline commands were removed (ADR 0003).
    print(f"Unknown command: {arguments[0]}. Run devroster --help.")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
