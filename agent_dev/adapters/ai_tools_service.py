"""Operations behind the AI tools screen and `devroster ai`.

No values of API keys leave this module: reads report which keys are set, never what they are.
"""

from pathlib import Path

from agent_dev.adapters.ai_providers import API_PROVIDERS, CLI_PROVIDERS, PROVIDERS, metadata
from agent_dev.adapters.credential_store import CredentialStore
from agent_dev.adapters.environment import validate_key
from agent_dev.adapters.global_ai import (
    CREDENTIAL_CONNECTION,
    GlobalAi,
    normalize_tools,
    shared_keys,
)
from agent_dev.domain.errors import DomainError, ErrorCode


def _key_names(values):
    tools = values.get("tools", {})
    return sorted(
        {
            tools[p]["credential_ref"]
            for p in API_PROVIDERS
            if p in tools and tools[p].get("credential_ref")
        }
        | {PROVIDERS[p]["credential_ref"] for p in API_PROVIDERS if PROVIDERS[p]["credential_ref"]}
    )


def state() -> dict:
    store = GlobalAi()
    shared = store.read()
    keys = shared_keys()
    return {
        "revision": shared["revision"],
        "digest": shared["digest"],
        "values": shared["values"] or normalize_tools({}),
        "auth": store.auth(),
        "keys": {name: bool(keys.get(name)) for name in _key_names(shared["values"])},
        "providers": metadata(),
        "projects": projects_using(),
    }


def save(values, expected_revision) -> dict:
    result = GlobalAi().save(values, expected_revision)
    try:
        check_auth()  # Refresh login state for the commands just saved (best effort).
    except (DomainError, OSError):
        pass
    return dict(state(), saved_revision=result["revision"])


def _command(provider) -> str:
    tools = GlobalAi().read()["values"].get("tools", {})
    return str(tools.get(provider, {}).get("command") or PROVIDERS[provider]["command"])


def check_auth() -> dict:
    """Probe the app-private CLI logins with the shared commands and store the results."""
    from agent_dev.adapters.project_setup import auth_probe

    return GlobalAi().record_auth({p: auth_probe(p, _command(p)) for p in CLI_PROVIDERS})


def login(operation, payload, owner=None) -> dict:
    from agent_dev.adapters import cli_login
    from agent_dev.adapters.project_setup import auth_probe

    if operation == "start":
        provider = str(payload.get("provider", ""))
        if provider not in cli_login.LOGIN_ARGS:
            raise DomainError(ErrorCode.INVALID_INPUT, "로그인할 CLI 도구를 선택해주세요.")
        return cli_login.start(provider, _command(provider), owner=owner)
    job = (cli_login.cancel if operation == "cancel" else cli_login.status)(
        payload.get("job_id"), owner=owner
    )
    if job["done"]:
        job["logged_in"] = cli_login.auth_result(job["job_id"], auth_probe, owner=owner)
        provider = job.get("provider")
        if provider in CLI_PROVIDERS and type(job["logged_in"]) is bool:
            GlobalAi().record_auth({provider: job["logged_in"]})
    return job


def set_key(name, value=None, *, delete=False) -> dict:
    validate_key(name)
    if delete:
        CredentialStore.remove_shared_key(CREDENTIAL_CONNECTION, name)
    else:
        CredentialStore.save_shared(CREDENTIAL_CONNECTION, name, value)
    return state()["keys"]


def projects_using() -> list:
    """Registered projects whose latest profile uses the shared AI tools."""
    from agent_dev.adapters.project_setup import ProjectSetup
    from agent_dev.adapters.workspace_registry import roots

    rows = []
    try:
        registered = roots()
    except (TypeError, ValueError, OSError):
        return rows
    for root in registered:
        try:
            profile = ProjectSetup(Path(root)).current_profile()[2]
        except (DomainError, OSError, ValueError, KeyError):
            continue
        if isinstance((profile or {}).get("ai_tools"), dict):
            rows.append({"name": Path(root).name, "root": str(root)})
    return rows


def import_project(root, *, apply=False) -> dict:
    """Move a project onto the shared AI tools without changing what it runs.

    With no shared AI yet, the project's runner settings become shared revision 1. The
    project then selects its enabled runners in their current order. Differences with an
    existing shared revision are reported so nothing changes silently.
    """
    from agent_dev.adapters.project_setup import ProjectSetup, auth_probe

    setup = ProjectSetup(Path(root))
    value = setup.read()
    _, _, profile = setup.current_profile()
    order = [r["provider"] for r in (profile or {}).get("runners", []) if r.get("enabled")]
    order = order or ([value["ai"]["provider"]] if value["ai"]["runners"][value["ai"]["provider"]]["enabled"] else [])
    store = GlobalAi()
    shared = store.read()
    differences = []
    if shared["revision"]:
        tools = shared["values"]["tools"]
        for provider in order:
            own, common = value["ai"]["runners"][provider], tools[provider]
            for field in sorted(set(own) | set(common)):
                if field != "enabled" and own.get(field) != common.get(field):
                    differences.append(
                        {"provider": provider, "field": field, "project": own.get(field), "shared": common.get(field)}
                    )
            if not common["enabled"]:
                differences.append(
                    {"provider": provider, "field": "enabled", "project": True, "shared": False}
                )
        new_shared = None
    else:
        new_shared = normalize_tools(
            {"tools": value["ai"]["runners"], "order": order + [p for p in PROVIDERS if p not in order]}
        )
    report = {
        "project": value["project_id"],
        "selected": order,
        "creates_shared": new_shared is not None,
        "shared_revision": shared["revision"],
        "differences": differences,
        "applied": False,
    }
    if not apply:
        return report
    if new_shared is not None:
        saved = store.save(new_shared, 0)
        report["shared_revision"] = saved["revision"]
        store.record_auth({p: auth_probe(p, new_shared["tools"][p]["command"]) for p in order if p in CLI_PROVIDERS})
    value["ai_tools"] = {"selected": order}
    result = setup.save(value)
    report.update(applied=True, profile_version=result["settings"]["profile_version"])
    return report


def run(args):
    """`devroster ai show|set FILE|import`."""
    import json

    if args.action == "show":
        return state()
    if args.action == "set":
        values = json.loads(Path(args.source).read_text(encoding="utf-8"))
        return save(values, GlobalAi().read()["revision"] if args.expected_revision is None else args.expected_revision)
    return import_project(Path(args.root).resolve(), apply=args.apply)

