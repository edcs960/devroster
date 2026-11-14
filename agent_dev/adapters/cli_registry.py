"""What the entry point plugs into the CLI: web screens and commands of upper modules.

The ui layer sits outside adapters, and a few command handlers (the Slack listener) sit above
the command line in the import order, so the entry point (bootstrap) registers them here
instead of the command line importing them. Modules that need ``web_server`` import this
small module, not the command line.
"""

from agent_dev.domain.errors import DomainError, ErrorCode

# Web screens the CLI can start ("studio", "dashboard", "setup").
WEB_SERVERS: dict = {}
# namespace -> handler(args) for commands whose module imports the command line.
COMMANDS: dict = {}
NOT_REGISTERED = "웹 화면은 devroster 명령(설치된 실행 파일)으로 시작해주세요."


def register_web_servers(servers) -> None:
    WEB_SERVERS.update(servers)


def register_commands(handlers) -> None:
    COMMANDS.update(handlers)


def web_server(name):
    serve = WEB_SERVERS.get(name)
    if serve is None:
        raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE, NOT_REGISTERED)
    return serve


def command(namespace):
    handler = COMMANDS.get(namespace)
    if handler is None:
        raise DomainError(
            ErrorCode.CAPABILITY_UNAVAILABLE, f"`{namespace}` 명령은 devroster 명령(설치된 실행 파일)으로 실행해주세요."
        )
    return handler
