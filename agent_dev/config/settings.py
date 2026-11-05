"""Shared settings service used by both the CLI (`devroster config ...`) and the
mascot web UI (`/api/settings`).

Settings live in three places and this module is the single writer for all of
them so the two front-ends stay consistent:

- ``config/runtime.json`` : repositories, database_access, ...
- ``<agent-dev-root>/.env``: environment variables / secrets (loaded by the
  CLI runners via ``load_cli_env_files``)
- ``<project-root>/.mcp.json``: MCP servers exposed to the Claude CLI runner
"""

from __future__ import annotations

import re

from agent_dev.config.managed_settings import ManagedSetting, managed_settings
from agent_dev.core.io import read_json, write_json
from agent_dev.core.paths import AGENT_DEV_ROOT, CONFIG_DIR, PROJECT_ROOT

ENV_PATH = AGENT_DEV_ROOT / ".env"
MCP_CONFIG_PATH = PROJECT_ROOT / ".mcp.json"
RUNTIME_PATH = CONFIG_DIR / "runtime.json"

# Secrets are masked at query boundaries; masks preserve existing credentials on save.
SECRET_ENV_PATTERN = re.compile(
    r"(^|_)(TOKEN|KEY|SECRET|PASSWORD|PAT|CREDENTIAL)(_|$)", re.IGNORECASE
)
DISCORD_ENV_KEYS = {
    "bot_token": "DISCORD_BOT_TOKEN",
    "channel_id": "DISCORD_CHANNEL_ID",
    "command_prefix": "DISCORD_COMMAND_PREFIX",
    "allowed_user_ids": "DISCORD_ALLOWED_USER_IDS",
    "allowed_commands": "DISCORD_ALLOWED_COMMANDS",
    "poll_interval_seconds": "DISCORD_COMMAND_POLL_INTERVAL_SECONDS",
    "fetch_limit": "DISCORD_COMMAND_FETCH_LIMIT",
    "timeout_seconds": "DISCORD_COMMAND_TIMEOUT_SECONDS",
}


def is_secret_key(key: str) -> bool:
    return bool(SECRET_ENV_PATTERN.search(key))


# --------------------------------------------------------------------------- #
# .env management
# --------------------------------------------------------------------------- #


def read_env_pairs() -> dict[str, str]:
    """Return KEY -> value pairs from .env (comments/blank lines ignored)."""
    pairs: dict[str, str] = {}
    if not ENV_PATH.exists():
        return pairs
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        pairs[key.strip()] = value.strip().strip('"').strip("'")
    return pairs


def discord_settings_from_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return Discord bot/listener settings managed through .env."""
    env = env if env is not None else read_env_pairs()
    return {name: env.get(key, "") for name, key in DISCORD_ENV_KEYS.items()}


def set_env_vars(updates: dict[str, str]) -> list[str]:
    """Upsert keys in .env, preserving existing comments and line order.

    Returns the list of keys that were written.
    """
    updates = {str(k).strip(): str(v) for k, v in updates.items() if str(k).strip()}
    if not updates:
        return []
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = {k: v for k, v in updates.items() if v not in ("****", "[REDACTED]", "<redacted>")}
    new_lines: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
                continue
        new_lines.append(raw_line)
    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return list(updates.keys())


def remove_env_var(key: str) -> bool:
    """Delete a key from .env. Returns True if it existed."""
    key = str(key).strip()
    if not key or not ENV_PATH.exists():
        return False
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    kept: list[str] = []
    removed = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if (
            stripped
            and not stripped.startswith("#")
            and "=" in stripped
            and stripped.split("=", 1)[0].strip() == key
        ):
            removed = True
            continue
        kept.append(raw_line)
    if removed:
        ENV_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return removed


# --------------------------------------------------------------------------- #
# .mcp.json management
# --------------------------------------------------------------------------- #


def read_mcp_servers() -> dict:
    if not MCP_CONFIG_PATH.exists():
        return {}
    data = read_json(MCP_CONFIG_PATH)
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}


def replace_mcp_servers(servers: dict) -> None:
    """Replace the full mcpServers map (used by the mascot JSON editor)."""
    if not isinstance(servers, dict):
        raise ValueError("mcpServers는 객체(JSON object)여야 합니다.")  # noqa: TRY004 - public input-error contract
    data = read_json(MCP_CONFIG_PATH) if MCP_CONFIG_PATH.exists() else {}
    if not isinstance(data, dict):
        data = {}
    data["mcpServers"] = preserve_masked(servers, data.get("mcpServers", {}))
    write_json(MCP_CONFIG_PATH, data)


def set_mcp_server(name: str, spec: dict) -> None:
    name = str(name).strip()
    if not name:
        raise ValueError("MCP 서버 이름이 필요합니다.")
    servers = read_mcp_servers()
    servers[name] = spec
    replace_mcp_servers(servers)


def remove_mcp_server(name: str) -> bool:
    name = str(name).strip()
    servers = read_mcp_servers()
    if name not in servers:
        return False
    del servers[name]
    replace_mcp_servers(servers)
    return True


def build_mcp_spec(
    *,
    url: str = "",
    headers: dict[str, str] | None = None,
    command: str = "",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Build an MCP server spec from CLI-style arguments.

    Provide either ``url`` for an http server or ``command`` for a stdio server.
    """
    url = url.strip()
    command = command.strip()
    if url and command:
        raise ValueError("MCP 서버는 --url 또는 --command 중 하나만 지정해야 합니다.")
    if url:
        spec: dict = {"type": "http", "url": url}
        if headers:
            spec["headers"] = headers
        return spec
    if command:
        spec = {"type": "stdio", "command": command}
        if args:
            spec["args"] = list(args)
        if env:
            spec["env"] = env
        return spec
    raise ValueError("MCP 서버에는 --url 또는 --command 가 필요합니다.")


# --------------------------------------------------------------------------- #
# runtime.json management
# --------------------------------------------------------------------------- #


def set_runtime_value(dotted_key: str, value) -> None:
    """Set a value in runtime.json addressed by a dotted key (e.g. 'database_access.mode')."""
    parts = [part for part in str(dotted_key).split(".") if part]
    if not parts:
        raise ValueError("설정 키가 필요합니다. 예: database_access.mode")
    runtime = read_json(RUNTIME_PATH)
    if not isinstance(runtime, dict):
        raise ValueError("runtime.json 형식이 올바르지 않습니다.")  # noqa: TRY004 - public input-error contract
    node = runtime
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value
    write_json(RUNTIME_PATH, runtime)


# --------------------------------------------------------------------------- #
# Unified snapshot
# --------------------------------------------------------------------------- #


def settings_snapshot() -> dict:
    """Return the full settings view shared by CLI `config show` and the mascot."""
    runtime = read_json(RUNTIME_PATH)
    if not isinstance(runtime, dict):
        runtime = {}
    env = read_env_pairs()
    mcp_servers = read_mcp_servers()
    snapshot = {
        "output": runtime.get("output", {}),
        "repositories": runtime.get("repositories", {}),
        "database_access": runtime.get("database_access", {}),
        "discord": discord_settings_from_env(env),
        "env": env,
        "mcp_servers": mcp_servers,
        "managed_settings": managed_settings_snapshot(runtime=runtime, env=env, mcp_servers=mcp_servers),
    }
    from agent_dev.domain.content import redact
    snapshot = redact(snapshot, tuple(v for k, v in env.items() if is_secret_key(k) and v))
    for item in snapshot["managed_settings"]:
        if item["secret"] and item["configured"]:
            item["value"] = "****"
    return snapshot



def managed_settings_snapshot(
    *,
    runtime: dict | None = None,
    env: dict[str, str] | None = None,
    mcp_servers: dict | None = None,
) -> list[dict]:
    runtime = runtime if runtime is not None else _read_runtime_dict()
    env = env if env is not None else read_env_pairs()
    mcp_servers = mcp_servers if mcp_servers is not None else read_mcp_servers()
    return [_managed_setting_view(item, runtime=runtime, env=env, mcp_servers=mcp_servers) for item in managed_settings()]


def _managed_setting_view(
    setting: ManagedSetting,
    *,
    runtime: dict,
    env: dict[str, str],
    mcp_servers: dict,
) -> dict:
    value = _managed_setting_value(setting, runtime=runtime, env=env, mcp_servers=mcp_servers)
    configured = _is_configured(value)
    return {
        "key": setting.key,
        "label": setting.label,
        "category": setting.category,
        "storage": setting.storage,
        "target": setting.target,
        "secret": setting.secret,
        "required_for": list(setting.required_for),
        "default": setting.default,
        "description": setting.description,
        "configured": configured,
        "value": value,
    }


def _managed_setting_value(setting: ManagedSetting, *, runtime: dict, env: dict[str, str], mcp_servers: dict):
    if setting.storage == ".env":
        return env.get(setting.target, "")
    if setting.storage == "runtime.json":
        return _get_dotted(runtime, setting.target)
    if setting.storage == ".mcp.json":
        if setting.target == "mcpServers":
            return sorted(mcp_servers.keys())
        return _get_dotted({"mcpServers": mcp_servers}, setting.target)
    return ""


def _get_dotted(data: dict, dotted_key: str):
    node = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return ""
        node = node[part]
    return node


def _is_configured(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _read_runtime_dict() -> dict:
    runtime = read_json(RUNTIME_PATH)
    return runtime if isinstance(runtime, dict) else {}


def preserve_masked(value, original):
    """Round-trip masked queries without replacing stored secrets by mask text."""
    if isinstance(value, dict):
        old = original if isinstance(original, dict) else {}
        return {key: preserve_masked(item, old.get(key)) for key, item in value.items()}
    if isinstance(value, list):
        old = original if isinstance(original, list) else []
        return [preserve_masked(item, old[index] if index < len(old) else None) for index, item in enumerate(value)]
    return original if value in ("****", "[REDACTED]", "<redacted>") else value
