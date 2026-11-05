"""Registry for user-managed local settings.

The registry describes *what* the user can configure and *where* the value is
stored. Values still live in local files (.env/runtime.json/.mcp.json); this
metadata gives CLI/UI/preflight a single source of truth for labels,
secret-ness, and feature ownership.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ManagedSetting:
    key: str
    label: str
    storage: str
    target: str
    category: str
    description: str
    secret: bool = False
    required_for: tuple[str, ...] = ()
    default: str = ""


MANAGED_SETTINGS: tuple[ManagedSetting, ...] = (
    ManagedSetting(
        key="project_profile",
        label="Default Project Profile",
        storage="runtime.json",
        target="project_profile",
        category="project",
        description="Default profile used when a task does not explicitly choose one.",
        required_for=("ask", "new-task", "dev-pipeline"),
    ),
    ManagedSetting(
        key="output.system_name",
        label="Output System Name",
        storage="runtime.json",
        target="output.system_name",
        category="output",
        description="System/product name used for output/<system-name>/docs and output/<system-name>/TASK-ID.",
        required_for=("ask", "new-task", "dev-pipeline"),
    ),
    ManagedSetting(
        key="runner",
        label="Default Runner",
        storage="runtime.json",
        target="runner",
        category="runner",
        description="Default execution runner.",
        required_for=("dev-pipeline",),
        default="hybrid_cli",
    ),
    ManagedSetting(
        key="repository.frontend.path",
        label="Frontend Repository Path",
        storage="runtime.json",
        target="repositories.frontend.path",
        category="repository",
        description="Local frontend repository path allowed for agent execution.",
        required_for=("frontend phases", "publish"),
    ),
    ManagedSetting(
        key="repository.backend.path",
        label="Backend Repository Path",
        storage="runtime.json",
        target="repositories.backend.path",
        category="repository",
        description="Local backend repository path allowed for agent execution.",
        required_for=("backend phases", "publish"),
    ),
    ManagedSetting(
        key="repository.agent.path",
        label="Agent Repository Path",
        storage="runtime.json",
        target="repositories.agent.path",
        category="repository",
        description="Local DevRoster repository path.",
        required_for=("agent-dev maintenance", "publish"),
    ),
    ManagedSetting(
        key="stitch.api_key",
        label="Stitch API Key",
        storage=".env",
        target="STITCH_API_KEY",
        category="stitch",
        description="API key used by Stitch MCP header-based access.",
        secret=True,
        required_for=("stitch MCP",),
    ),
    ManagedSetting(
        key="database.enabled",
        label="Use DB Context",
        storage="runtime.json",
        target="database_access.enabled",
        category="database",
        description="Whether read-only database context is injected into DB-related phases.",
        required_for=("dba", "backend phases"),
        default="false",
    ),
    ManagedSetting(
        key="database.backend_path",
        label="Backend Path for DB Snapshot",
        storage="runtime.json",
        target="database_access.backend_path",
        category="database",
        description="Backend path used to discover schema/migration context.",
        required_for=("dba", "backend phases"),
    ),
    ManagedSetting(
        key="database.docker_container",
        label="Database Docker Container",
        storage="runtime.json",
        target="database_access.docker_container",
        category="database",
        description="Docker container used for optional read-only DB schema snapshot.",
        required_for=("dba", "backend phases"),
    ),
    ManagedSetting(
        key="database.name",
        label="Database Name",
        storage="runtime.json",
        target="database_access.database",
        category="database",
        description="Database name used for optional schema snapshot.",
        required_for=("dba", "backend phases"),
    ),
    ManagedSetting(
        key="database.username",
        label="Database Username",
        storage="runtime.json",
        target="database_access.username",
        category="database",
        description="Database username used for optional schema snapshot.",
        required_for=("dba", "backend phases"),
    ),
    ManagedSetting(
        key="discord.bot_token",
        label="Discord Bot Token",
        storage=".env",
        target="DISCORD_BOT_TOKEN",
        category="discord",
        description="Bot token used for Discord notifications and command listener.",
        secret=True,
        required_for=("discord-listen", "discord notifications"),
    ),
    ManagedSetting(
        key="discord.channel_id",
        label="Discord Channel ID",
        storage=".env",
        target="DISCORD_CHANNEL_ID",
        category="discord",
        description="Channel where notifications are posted and commands are read.",
        required_for=("discord-listen", "discord notifications"),
    ),
    ManagedSetting(
        key="discord.command_prefix",
        label="Discord Command Prefix",
        storage=".env",
        target="DISCORD_COMMAND_PREFIX",
        category="discord",
        description="Prefix that marks Discord messages as DevRoster commands.",
        required_for=("discord-listen",),
        default="!dr",
    ),
    ManagedSetting(
        key="discord.allowed_user_ids",
        label="Discord Allowed User IDs",
        storage=".env",
        target="DISCORD_ALLOWED_USER_IDS",
        category="discord",
        description="Optional comma-separated allowlist of Discord user IDs.",
        required_for=("discord-listen",),
    ),
    ManagedSetting(
        key="discord.allowed_commands",
        label="Discord Allowed Commands",
        storage=".env",
        target="DISCORD_ALLOWED_COMMANDS",
        category="discord",
        description="Optional comma-separated allowlist of devroster subcommands allowed from Discord.",
        required_for=("discord-listen",),
    ),
    ManagedSetting(
        key="discord.poll_interval_seconds",
        label="Discord Poll Interval Seconds",
        storage=".env",
        target="DISCORD_COMMAND_POLL_INTERVAL_SECONDS",
        category="discord",
        description="Polling interval for the Discord REST listener.",
        required_for=("discord-listen",),
        default="5",
    ),
    ManagedSetting(
        key="github.pat_token",
        label="GitHub PAT Token",
        storage=".env",
        target="GITHUB_PAT_TOKEN",
        category="github",
        description="Token used by GitHub MCP or publish/PR flows when configured.",
        secret=True,
        required_for=("github MCP", "publish", "open-pr"),
    ),
    ManagedSetting(
        key="openai.api_key",
        label="OpenAI API Key",
        storage=".env",
        target="OPENAI_API_KEY",
        category="runner",
        description="API key used only when the openai runner is selected.",
        secret=True,
        required_for=("openai runner",),
    ),
    ManagedSetting(
        key="mcp.servers",
        label="MCP Servers",
        storage=".mcp.json",
        target="mcpServers",
        category="mcp",
        description="Shared MCP server definitions. Secrets should be referenced via ${ENV_VAR}.",
        required_for=("claude_cli MCP", "codex_cli MCP injection"),
    ),
)


def managed_settings() -> tuple[ManagedSetting, ...]:
    return MANAGED_SETTINGS
