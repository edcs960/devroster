"""AI tools shared by every project of one management root.

The AI tools screen saves runner settings (use, command, model, effort, routing and API
endpoints) as immutable revisions in ``global/settings.db``. A project only chooses which
of those tools it uses and in what order (profile key ``ai_tools.selected``). The runners
of a task are composed when its snapshot is created, so a new revision applies from the
next approved task while retries keep the frozen runners. API keys live in the shared
credential store under the ``ai-providers`` connection, never in a revision.
"""

import time
from pathlib import Path

from agent_dev.adapters.ai_providers import (
    API_PROVIDERS,
    CLI_PROVIDERS,
    PROVIDERS,
    credential_ready,
    defaults,
    normalize,
)
from agent_dev.adapters.global_settings import GlobalStore
from agent_dev.domain.content import canonical, digest
from agent_dev.domain.errors import DomainError, ErrorCode

CREDENTIAL_CONNECTION = "ai-providers"


def invalid(message):
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def normalize_tools(values) -> dict:
    """``{"tools": {provider: config}, "order": [provider, ...]}`` with every provider present."""
    if not isinstance(values, dict) or set(values) - {"tools", "order"}:
        invalid("AI 도구 설정 형식을 확인해주세요.")
    raw = values.get("tools", {})
    if not isinstance(raw, dict) or set(raw) - set(PROVIDERS):
        invalid("지원하는 AI 도구만 설정할 수 있습니다.")
    tools = {}
    for provider in PROVIDERS:
        config = normalize(provider, raw.get(provider, defaults(provider)))
        if provider in CLI_PROVIDERS:
            command = config["command"]
            # Shared settings have no project folder to resolve a relative path against.
            if ("/" in command or "\\" in command) and not Path(command).expanduser().is_absolute():
                invalid("공통 AI 도구의 실행파일은 이름(예: codex) 또는 절대 경로로 입력해주세요.")
            config["command"] = (
                str(Path(command).expanduser()) if command.startswith("~") else command
            )
        tools[provider] = config
    order = values.get("order", [])
    if (
        not isinstance(order, list)
        or not all(isinstance(p, str) for p in order)
        or set(order) - set(PROVIDERS)
    ):
        invalid("AI 도구 순서를 확인해주세요.")
    order = list(dict.fromkeys(order))
    order += [p for p in PROVIDERS if p not in order]
    return {"tools": tools, "order": order}


class GlobalAi(GlobalStore):
    def read(self, revision=None) -> dict:
        """``{"revision", "digest", "values"}``; revision 0 means no shared AI tools yet."""
        with self.reader() as conn:
            rows = []
            if conn is not None and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='global_ai_versions'"
            ).fetchone():
                if revision is None:
                    rows = conn.execute(
                        "SELECT * FROM global_ai_versions ORDER BY revision DESC LIMIT 1"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM global_ai_versions WHERE revision=?", (revision,)
                    ).fetchall()
        if not rows:
            if revision not in {None, 0}:
                invalid("선택한 AI 도구 revision이 없습니다.")
            return {"revision": 0, "digest": digest({}), "values": {}}
        import json

        row = rows[0]
        return {
            "revision": row["revision"],
            "digest": row["digest"],
            "values": json.loads(row["content_json"]),
        }

    def save(self, values, expected_revision: int) -> dict:
        normalized = normalize_tools(values)
        if type(expected_revision) is not int or expected_revision < 0:
            invalid("최신 AI 도구 설정을 불러온 뒤 저장해주세요.")
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT COALESCE(MAX(revision),0) FROM global_ai_versions"
            ).fetchone()[0]
            if current != expected_revision:
                raise DomainError(
                    ErrorCode.STALE_VERSION,
                    "다른 화면에서 AI 도구 설정이 바뀌었습니다. 다시 불러온 뒤 저장해주세요.",
                )
            revision, value_digest = current + 1, digest(normalized)
            conn.execute(
                "INSERT INTO global_ai_versions VALUES(?,?,?,?)",
                (revision, canonical(normalized), value_digest, time.time()),
            )
            self.event(
                conn,
                "GlobalAiSaved",
                revision,
                {
                    "digest": value_digest,
                    "enabled": [p for p in normalized["order"] if normalized["tools"][p]["enabled"]],
                },
            )
        return {"revision": revision, "digest": value_digest, "values": normalized}

    def auth(self) -> dict:
        """``{provider: {"authenticated": bool, "checked_at": float}}`` from the last checks."""
        with self.reader() as conn:
            if conn is None or not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='global_ai_auth'"
            ).fetchone():
                return {}
            return {
                row["provider"]: {
                    "authenticated": bool(row["authenticated"]),
                    "checked_at": row["checked_at"],
                }
                for row in conn.execute("SELECT * FROM global_ai_auth")
            }

    def record_auth(self, results: dict) -> dict:
        """Store login probe results ``{provider: bool}`` for CLI tools."""
        now = time.time()
        with self.transaction() as conn:
            for provider, authenticated in results.items():
                if provider not in CLI_PROVIDERS or type(authenticated) is not bool:
                    continue
                conn.execute(
                    "INSERT INTO global_ai_auth VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE "
                    "SET authenticated=excluded.authenticated, checked_at=excluded.checked_at",
                    (provider, int(authenticated), now),
                )
        return self.auth()


def selected_tools(profile) -> list | None:
    """The project's ordered AI tool choice, or None when it still uses its own runners."""
    value = (profile or {}).get("ai_tools")
    if not isinstance(value, dict):
        return None
    selected = value.get("selected", [])
    return [p for p in dict.fromkeys(selected) if p in PROVIDERS]


def validate_selection(value) -> dict:
    if not isinstance(value, dict) or set(value) - {"selected"}:
        invalid("사용할 AI 도구 선택 형식을 확인해주세요.")
    selected = value.get("selected", [])
    if (
        not isinstance(selected, list)
        or not all(isinstance(p, str) for p in selected)
        or set(selected) - set(PROVIDERS)
    ):
        invalid("사용할 AI 도구를 목록에서 선택해주세요.")
    return {"selected": list(dict.fromkeys(selected))}


def shared_keys() -> dict:
    """API key values saved on the AI tools screen (process use only, never persisted)."""
    from agent_dev.adapters.credential_store import CredentialStore

    try:
        return CredentialStore.shared_entry(CREDENTIAL_CONNECTION)
    except (DomainError, OSError, ValueError):
        return {}


def compose_runners(shared: dict, selected: list, auth: dict, environment=None) -> list:
    """Profile-style runner rows: the selected tools in order, each with its shared config.

    A tool switched off on the AI tools screen stays out even when a project selected it.
    """
    tools = shared["values"]["tools"]
    environment = environment or {}
    rows = []
    for provider in selected:
        config = dict(tools[provider])
        if not config.pop("enabled"):
            continue
        rows.append(
            {
                "id": provider.removesuffix("_cli"),
                "provider": provider,
                "enabled": True,
                "authenticated": credential_ready(provider, config, environment)
                if provider in API_PROVIDERS
                else auth.get(provider, {}).get("authenticated") is True,
                "capabilities": ["text.generate"]
                if provider in API_PROVIDERS
                else ["code.read", "code.write", "text.generate"],
                "config": config,
            }
        )
    return rows


def effective_runners(profile, environment=None):
    """(runners, origin) for a project, or (None, None) when it keeps its own runners."""
    selected = selected_tools(profile)
    if selected is None:
        return None, None
    store = GlobalAi()
    shared = store.read()
    if not shared["revision"]:
        raise DomainError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "AI 도구 메뉴에서 공통 AI 도구를 먼저 설정해주세요.",
        )
    keys = shared_keys()
    env = {**keys, **(environment or {})}
    auth = store.auth()
    unchecked = [
        p
        for p in selected
        if p in CLI_PROVIDERS and shared["values"]["tools"][p]["enabled"] and p not in auth
    ]
    if unchecked:
        # A tool nobody has checked yet would otherwise be frozen as "not logged in" and
        # silently dropped from the runner candidates. Probe it once and remember the result.
        from agent_dev.adapters.project_setup import auth_probe

        tools = shared["values"]["tools"]
        auth = store.record_auth({p: auth_probe(p, tools[p]["command"]) for p in unchecked})
    runners = compose_runners(shared, selected, auth, env)
    origin = {
        "global_ai_revision": shared["revision"],
        "global_ai_digest": shared["digest"],
        "selected": selected,
    }
    return runners, origin


def legacy_runtime(runtime: dict) -> dict:
    """Legacy runtime with the project's chosen shared AI tools; the file itself is unchanged.

    Used when a legacy run starts, so a new shared revision applies from the next run.
    Projects without ``ai_tools`` or an empty shared store keep their runtime as saved.
    """
    root = runtime.get("setup_configuration_root")
    if not root:
        return runtime
    try:
        from agent_dev.adapters.project_setup import ProjectSetup

        profile = ProjectSetup(Path(root)).current_profile()[2]
        selected = selected_tools(profile)
        if selected is None:
            return runtime
        store = GlobalAi()
        shared = store.read()
    except (DomainError, OSError, ValueError):
        return runtime
    if not shared["revision"]:
        return runtime
    import copy

    result = copy.deepcopy(runtime)
    tools = shared["values"]["tools"]
    chosen = [p for p in selected if tools[p]["enabled"]]
    cli = [p for p in chosen if p in CLI_PROVIDERS]
    if cli:
        result["runner"] = cli[0]
        result["runner_failover"] = dict(
            result.get("runner_failover", {}), enabled=len(cli) > 1, order=cli
        )
    for provider in chosen:
        config = {k: v for k, v in tools[provider].items() if k != "enabled"}
        result[provider] = dict(result.get(provider, {}), **config)
    result["ai_settings_origin"] = {
        "global_ai_revision": shared["revision"],
        "global_ai_digest": shared["digest"],
        "selected": selected,
    }
    return result

