"""`devroster skills ...`: catalog, stack suggestions, repository status and sync."""

from pathlib import Path

from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.skill_store import SkillStore, cache_root, catalog, detect
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.skill_selection import repository_skills, sanitize, suggest


def _profile(root):
    setup = ProjectSetup(root)
    project_id, version, profile = setup.current_profile()
    if not profile:
        raise DomainError(
            ErrorCode.INVALID_INPUT, "프로젝트 설정을 먼저 저장해주세요 (devroster setup)."
        )
    # Module folders (stack detection) and where their skills are installed (git top level).
    modules = {
        module: (setup.root / binding["path"]).resolve()
        for module, binding in profile.get("repositories", {}).items()
    }
    return setup, project_id, version, profile, modules


def install_root(path: Path) -> Path:
    """Agents look for skills up to the git top level, so install there (monorepo modules)."""
    import subprocess

    try:
        top = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        top = ""
    return Path(top).resolve() if top else path


def listing():
    data = catalog()
    return {
        "bundles": {
            name: {
                "description": bundle["description"],
                "skills": [n for n, s in data["skills"].items() if name in s["bundles"]],
            }
            for name, bundle in data["bundles"].items()
        },
        "skills": {
            name: {
                "bundles": skill["bundles"],
                "roles": skill["roles"],
                "source": skill["source"],
                "license": data["sources"][skill["source"]]["license"],
            }
            for name, skill in data["skills"].items()
        },
    }


def suggestions(root, modules=None):
    """Per module of the setup screen: detected stack markers and proposed bundles.

    ``modules`` ([{id, path}]) are the unsaved modules on screen; saved modules otherwise.
    """
    setup = ProjectSetup(root)
    value = setup.read()
    current = value.get("skills") or {}
    if modules is not None:
        if not isinstance(modules, list) or len(modules) > 50 or not all(
            isinstance(m, dict) and isinstance(m.get("id"), str) and isinstance(m.get("path"), str)
            for m in modules
        ):
            raise DomainError(ErrorCode.INVALID_INPUT, "모듈 목록을 확인해주세요.")
        modules = [m for m in modules if m["id"] and m["path"]]
    result = {}
    for module in value.get("modules", []) if modules is None else modules:
        path = (setup.root / module["path"]).resolve()
        markers = detect(path)
        result[module["id"]] = {
            "path": str(path),
            "markers": markers,
            "suggested": suggest(markers),
            "selected": list(
                dict.fromkeys(
                    current.get("bundles", [])
                    + current.get("repositories", {}).get(module["id"], {}).get("bundles", [])
                )
            ),
        }
    return result


def _plan(root):
    _, project_id, version, profile, modules = _profile(root)
    repositories = {module: install_root(path) for module, path in modules.items()}
    data = catalog()
    settings, _ = sanitize(data, profile.get("skills", {}))
    plan = {
        module: repository_skills(data, settings, module) if settings else []
        for module in repositories
    }
    return project_id, version, repositories, plan, data


def status(root):
    project_id, version, repositories, plan, data = _plan(root)
    store = SkillStore(cache_root(), data)
    return {
        "project_id": project_id,
        "profile_version": version,
        "repositories": {
            module: {
                "path": str(path),
                "skills": store.status(path, _union(repositories, plan, path)),
            }
            for module, path in repositories.items()
        },
    }


def _union(repositories, plan, path):
    """Modules sharing one repository install the union of their skills there."""
    return list(dict.fromkeys(n for m, p in repositories.items() if p == path for n in plan[m]))


def sync(root, *, dry_run=False):
    """Install each repository's selected skills; managed skills no longer selected are removed."""
    project_id, version, repositories, plan, data = _plan(root)
    if dry_run:
        return {"project_id": project_id, "profile_version": version, "plan": plan}
    store = SkillStore(cache_root(), data)
    reports = {}
    seen = set()
    for module, path in repositories.items():
        if path in seen:
            continue  # Two modules can share one repository folder; install the union once.
        seen.add(path)
        names = _union(repositories, plan, path)
        reports[module] = dict(store.sync_repository(path, names), path=str(path))
    setup = ProjectSetup(root)
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository

    if setup.db_path.is_file():
        WorkflowRepository(Database(setup.db_path)).event(
            project_id,
            "SkillsSynced",
            {
                "profile_version": version,
                "repositories": {
                    module: {
                        k: report[k] for k in ("installed", "unchanged", "removed", "conflicts")
                    }
                    | {"failed": sorted(report["failed"])}
                    for module, report in reports.items()
                },
            },
        )
    return {"project_id": project_id, "profile_version": version, "repositories": reports}


def run(args):
    root = Path(args.root).resolve()
    if args.action == "catalog":
        return listing()
    if args.action == "suggest":
        return suggestions(root)
    if args.action == "status":
        return status(root)
    return sync(root, dry_run=args.dry_run)
