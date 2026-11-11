"""Composition and configuration resolution live outside the application layer."""

import json
import shutil
from contextlib import ExitStack
from pathlib import Path

from agent_dev.adapters.artifacts import FilesystemArtifacts
from agent_dev.adapters.cli_runner import CliRunnerAdapter
from agent_dev.adapters.governance import GovernanceRepository
from agent_dev.adapters.harness import parse_harness
from agent_dev.adapters.phase_catalog import SnapshotPhaseCatalog
from agent_dev.adapters.runner_registry import CapabilityRunnerRegistry, RunnerCandidate
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.verification import VerificationAdapter
from agent_dev.adapters.wiki_knowledge import WikiKnowledge
from agent_dev.adapters.worktrees import WorktreeManager
from agent_dev.application.workflow import WorkflowCoordinator
from agent_dev.domain.contracts import ToolConnection
from agent_dev.domain.errors import DomainError, ErrorCode


def project_root(configuration_root):
    from agent_dev.adapters.storage_registry import workspace_locations
    from agent_dev.adapters.workspace import configuration_root as normalize
    from agent_dev.adapters.workspace import metadata

    configuration_root = normalize(configuration_root)
    metadata(configuration_root)  # Reject an unsupported marker before resolving.
    return workspace_locations(configuration_root).source_root


ROLE_OVERRIDE_DIR = ".agent-dev/roles"
ROLE_OVERRIDE_LIMIT = 64 * 1024


def role_override(root, name):
    """(text, source) from the workspace override folder, or None when no override exists.

    The folder is DevRoster-managed configuration, not repository content, so a role edited
    inside a task worktree can never redefine the rules that judge it. ``root`` is the
    configuration root; an external workspace records where its override came from.
    """
    from agent_dev.adapters.storage_registry import workspace_locations
    from agent_dev.adapters.workspace import configuration_root
    from agent_dev.core.storage_locations import EXTERNAL
    from agent_dev.domain.content import content_digest

    locations = workspace_locations(configuration_root(Path(root)))
    directory = locations.role_dir
    path = directory / f"{name}.md"
    external = locations.layout == EXTERNAL
    relative = f"roles/{name}.md" if external else f"{ROLE_OVERRIDE_DIR}/{name}.md"
    if not path.exists() and not path.is_symlink():
        return None
    if (
        directory.is_symlink()
        or path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(directory.resolve())
    ):
        raise DomainError(
            ErrorCode.POLICY_VIOLATION, "Role override must be a regular file: " + relative
        )
    data = path.read_bytes()
    if len(data) > ROLE_OVERRIDE_LIMIT:
        raise DomainError(ErrorCode.INVALID_INPUT, "Role override exceeds 64KB: " + relative)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise DomainError(ErrorCode.INVALID_INPUT, "Role override must be UTF-8: " + relative) from None
    source = {"source": "override", "path": relative, "digest": content_digest(data)}
    if external:
        source.update(location="config", workspace_id=locations.workspace_id)
    return text, source


PROFILE_KEYS = {
    "repositories",
    "phases",
    "role_context",
    "verification",
    "runners",
    "allowed_paths",
    "tools",
    "schema_version",
    "notification",
    "retention",
    "knowledge",
    "execution_limits",
    "context_policy",
    "output_language",
    "environment_keys",
    "workflow",
    "role_files",
    "test_paths",
    "document_paths",
    "design_references",
    "roles",
    "preview_services",
    "usage_policy",
    "skills",
    "ai_tools",
    "wiki_dir",
    "completion",
}


def merge(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else value
        )
    return result


def project_domain(resolved):
    """The bundled domain pack for a project: its saved roster domain, else "fmea" when the
    workflow includes the FMEA Expert (profiles saved before rosters existed)."""
    from agent_dev import defaults

    roles = resolved.get("roles")
    if isinstance(roles, dict) and roles.get("domain"):
        return defaults.domain_name(roles["domain"])
    nodes = resolved.get("workflow_graph", {}).get("nodes", [])
    if any(n.get("role_id") == "fmea-expert" for n in nodes):
        return defaults.domain_name("fmea")
    return defaults.GENERIC


def approved_skills(resolved):
    """Per role node: catalog skills of the node's repository bundles, cached and verified."""
    settings = resolved.get("skills") or {}
    if not settings:
        return {}
    from agent_dev.adapters.skill_store import SkillStore, cache_root, catalog
    from agent_dev.domain.skill_selection import apply_limits, role_candidates, sanitize

    data = catalog()
    settings, warnings = sanitize(data, settings)
    store = SkillStore(cache_root(), data)
    items, missing, nodes = {}, set(), {}
    for node in resolved.get("workflow_graph", {}).get("nodes", []):
        if node.get("kind") != "role":
            continue
        module = node.get("module") or resolved.get("execution_module")
        available = []
        for name in role_candidates(data, settings, node["role_id"], module):
            if name not in items and name not in missing:
                path = store.cached(name)
                if path is None:
                    missing.add(name)
                    warnings.append(f"Skill {name} is not synced; run `devroster skills sync`.")
                    continue
                items[name] = {
                    "description": store.description(name),
                    "path": str(path),
                    "tree_digest": store.expected_digest(name),
                    "license": data["sources"][data["skills"][name]["source"]]["license"],
                }
            if name in items:
                available.append(name)
        descriptions = {name: items[name]["description"] for name in available}
        nodes[node["id"]], limited = apply_limits(node["role_id"], available, descriptions)
        warnings += limited
    used = {name for names in nodes.values() for name in names}
    return {
        "items": {name: item for name, item in items.items() if name in used},
        "nodes": nodes,
        "warnings": list(dict.fromkeys(warnings)),
    }


def project_checks(resolved):
    """The approved verification commands, bound to the task worktrees, for check_server."""
    bindings = resolved.get("repositories", {})
    checks = []
    for item in resolved.get("verification", {}).get("commands", []):
        binding = bindings.get(item.get("repository"))
        command = item.get("command")
        if not binding or not isinstance(command, list) or not command:
            continue
        root = Path(binding["worktree"]).resolve()
        cwd = (root / item.get("cwd", ".")).resolve()
        if not cwd.is_relative_to(root):
            continue
        checks.append(
            {
                "name": item["name"],
                "command": command,
                "cwd": str(cwd),
                "timeout": int(item.get("timeout_seconds", 900)),
                # Names only: the values reach check_server through the runner's environment.
                "environment_keys": list(resolved.get("environment_keys", [])),
            }
        )
    return checks


class WorkflowRuntime:
    def __init__(self, root: Path, db_path=None, secrets=()):
        from agent_dev.adapters.workspace import configuration_root, engine_configuration

        # The project folder of a legacy layout resolves to the engine's settings, so the
        # v2 flags and runners are the same whichever folder a command names.
        self.root = engine_configuration(configuration_root(root))
        if (self.root / "upgrade-journal.json").exists():
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "Interrupted upgrade requires devroster upgrade --recover.",
            )
        self.project_root = project_root(self.root)
        from agent_dev.adapters.secrets import registered_secrets
        from agent_dev.adapters.storage_registry import workspace_locations

        self.locations = workspace_locations(self.root)
        if (
            db_path is None
            and self.locations.workspace_id
            and not self.locations.state_db.is_file()
        ):
            # A registered location never gets a fresh, empty canonical DB (AC-12).
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                f"등록된 설정 위치에 작업 DB가 없습니다: {self.locations.state_db}. "
                "위치를 복구하거나 백업에서 복원해주세요.",
            )
        secrets = tuple(set(secrets) | set(registered_secrets(self.root)))
        self.store = WorkflowRepository(
            Database(db_path or self.locations.state_db), secrets=secrets
        )
        self.safety = ExecutionSafety(self.store)
        self.artifacts = FilesystemArtifacts(self.store, self.locations.output_root)
        self.governance = GovernanceRepository(self.store, self.project_root)
        self.wiki = WikiKnowledge(self.store, self.project_root)
        from agent_dev.adapters.ai_hooks import HOOKS_FILE, HookObserver

        if (self.project_root / HOOKS_FILE).is_file():
            # Record-only hook moments follow the task events (a task without hooks skips).
            self.store.hook_observer = HookObserver(self.task_hooks)

    def flags(self):
        path = self.root / "config/runtime.json"
        return (
            json.loads(path.read_text(encoding="utf-8")).get("orchestrator_v2", {})
            if path.exists()
            else {}
        )

    def require_flag(self, name):
        if self.flags().get(name) is not True:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, f"orchestrator_v2.{name} must be explicitly enabled."
            )

    def resolve_snapshot(self, task_id):
        from agent_dev.adapters import workspace_trust

        workspace_trust.require(self.root, "작업 승인")
        task = self.store.task(task_id)
        plan = self.store.db.rows("SELECT * FROM task_plans WHERE id=?", (task.plan_id,))[0]
        profiles = self.store.db.rows(
            "SELECT * FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (task.project_id,),
        )
        scope = task.definition.get("handoff_scope")
        if scope:
            profiles = self.store.db.rows(
                "SELECT * FROM project_profile_versions WHERE project_id=? AND version=?",
                (task.project_id, scope["profile_version"]),
            )
            if not profiles or profiles[0]["digest"] != scope["profile_digest"]:
                raise DomainError(ErrorCode.POLICY_VIOLATION, "Frozen handoff profile changed.")
        profile = json.loads(profiles[0]["content_json"])
        override = task.definition.get("config_override", {})
        if set(profile) - PROFILE_KEYS or set(override) - PROFILE_KEYS:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown profile configuration key.")
        resolved = merge(
            {"schema_version": 1, "tools": [], "allowed_paths": []}, merge(profile, override)
        )
        # A project that chose shared AI tools gets them as of now; retries keep this list.
        from agent_dev.adapters.global_ai import effective_runners

        runners, ai_origin = effective_runners(resolved)
        if runners is not None:
            resolved["runners"] = runners
            resolved["ai_settings_origin"] = ai_origin
        if scope:
            module = scope["module_id"]
            if module not in resolved.get("repositories", {}):
                raise DomainError(ErrorCode.POLICY_VIOLATION, "Frozen handoff module unavailable.")
            binding = resolved["repositories"][module]
            # Only the handoff module gets a worktree and a repository lease, so tasks
            # for other modules run in parallel.
            resolved["repositories"] = {
                module: dict(binding, allowed_paths=scope["allowed_paths"])
            }
            resolved["execution_module"] = module
            resolved["allowed_paths"] = scope["allowed_paths"]
            verification = resolved.get("verification", {})
            resolved["verification"] = dict(
                verification,
                commands=[
                    c
                    for c in verification.get("commands", [])
                    if c["name"] in scope["verification_gates"] and c.get("repository") == module
                ],
                required_gates=[
                    g
                    for g in verification.get("required_gates", [])
                    if g in scope["verification_gates"]
                ],
            )
            resolved["phases"] = [
                dict(phase, module=module) for phase in resolved.get("phases", [])
            ]
            if resolved.get("workflow"):
                # Preserve the registered immutable graph; reject cross-module role scope.
                selected_graph = resolved["workflow"].get("document", resolved["workflow"])
                if any(
                    n.get("module") not in {None, module} for n in selected_graph.get("nodes", [])
                ):
                    raise DomainError(
                        ErrorCode.POLICY_VIOLATION, "Workflow includes a different handoff module."
                    )
        from agent_dev.adapters.workflow_definitions import check_custom_roles, trusted_metadata
        from agent_dev.domain.role_policy import legacy_role, policy
        from agent_dev.domain.workflow_graph import legacy, normalize, preview

        if not resolved.get("allowed_paths"):
            resolved["allowed_paths"] = list(
                dict.fromkeys(
                    path
                    for binding in resolved.get("repositories", {}).values()
                    for path in binding.get("allowed_paths", [])
                )
            )
        selected = resolved.get("workflow")
        if selected:
            graph = normalize(
                selected.get("document", selected),
                modules=set(resolved.get("repositories", {})),
                gates={c["name"] for c in resolved.get("verification", {}).get("commands", [])},
            )
            if selected.get("digest") and selected["digest"] != __import__(
                "agent_dev.domain.content", fromlist=["digest"]
            ).digest(graph):
                raise DomainError(ErrorCode.POLICY_VIOLATION, "Workflow version digest mismatch.")
            check_custom_roles(graph, resolved)
            metadata = trusted_metadata(task.definition)
            missing = preview(graph, metadata)["missing_roles"]
            if missing:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION,
                    "Workflow required roles missing: " + ", ".join(missing),
                )
            resolved["workflow_graph"] = graph
            resolved["workflow_metadata"] = metadata
            resolved["workflow_legacy"] = False
            resolved["phases"] = [
                {
                    "id": n["id"],
                    "role_id": n["role_id"],
                    "role_file": n.get(
                        "role_file", resolved.get("role_files", {}).get(n["role_id"], "")
                    ),
                    "capabilities": policy(n["role_id"])["capabilities"],
                }
                for n in graph["nodes"]
                if n["kind"] == "role"
            ]
            resolved["role_policies"] = {
                n["id"]: policy(
                    n["role_id"],
                    project_paths=resolved.get("allowed_paths", []),
                    module_paths=resolved.get("repositories", {})
                    .get(n.get("module"), {})
                    .get("allowed_paths", []),
                    node_paths=n.get("allowed_paths", []),
                    test_paths=resolved.get("test_paths", []),
                    document_paths=resolved.get("document_paths", []),
                )
                for n in graph["nodes"]
                if n["kind"] == "role"
            }
        else:
            legacy_phases: list[dict] = [
                {"id": p} if isinstance(p, str) else p for p in resolved.get("phases", [])
            ]
            resolved["phases"] = legacy_phases
            resolved["workflow_graph"] = legacy(resolved.get("phases", []))
            resolved["workflow_legacy"] = True
            resolved["role_policies"] = {}
            for phase, node in zip(
                resolved["phases"], resolved["workflow_graph"]["nodes"], strict=True
            ):
                canonical = legacy_role(phase)
                node["role_id"] = canonical
                authority = policy(
                    canonical,
                    project_paths=resolved.get("allowed_paths", []),
                    module_paths=resolved.get("repositories", {})
                    .get(phase.get("module"), {})
                    .get("allowed_paths", []),
                    node_paths=phase.get("allowed_paths", []),
                    test_paths=resolved.get("test_paths", []),
                    document_paths=resolved.get("document_paths", []),
                )
                requested = set(phase.get("capabilities", ["code.read"]))
                allowed = set(authority["capabilities"])
                phase["capabilities"] = sorted(requested & allowed) or ["code.read"]
                if requested == {"text.generate"} and authority["mode"] != "code":
                    phase["capabilities"] = ["text.generate"]
                resolved["role_policies"][phase["id"]] = authority
                if phase.get("module"):
                    node["module"] = phase["module"]
        if resolved.get("workflow_legacy", False):
            resolved["workflow_metadata"] = trusted_metadata(task.definition)
            missing = preview(resolved["workflow_graph"], resolved["workflow_metadata"])[
                "missing_roles"
            ]
            if missing:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION,
                    "Workflow required roles missing: " + ", ".join(missing),
                )
            for phase in resolved["phases"]:
                authority = resolved["role_policies"][phase["id"]]
                if authority["mode"] == "code" and "code.write" not in phase["capabilities"]:
                    authority["mode"] = "readonly"
                    authority["capabilities"] = phase["capabilities"]
        if scope:
            for node in resolved["workflow_graph"]["nodes"]:
                if node["kind"] == "role":
                    node["module"] = scope["module_id"]
            for authority in resolved["role_policies"].values():
                authority["scopes"].append(scope["allowed_paths"])
        if (not resolved.get("phases") and not selected) or not resolved.get("repositories"):
            raise DomainError(
                ErrorCode.INVALID_INPUT, "Profile requires phases and repository bindings."
            )
        from agent_dev.domain.execution_limits import limits

        resolved["execution_limits"] = limits(resolved.get("execution_limits"))
        # Role files become approved snapshot text rather than live reads during an attempt.
        # Rules and role text come from the bundled defaults, never the repository; only the
        # workspace override folder may replace a built-in role (ADR 0002 D1).
        from agent_dev import defaults

        role_context = dict(resolved.get("role_context", {}))
        role_sources = {}
        domain = project_domain(resolved)

        def built_in_text(node_id, name, path):
            override = role_override(self.root, defaults.role_name(name))
            if override is None:
                return path.read_text(encoding="utf-8")
            role_sources[node_id] = override[1]
            return override[0]

        if not resolved.get("workflow_legacy", True):
            from agent_dev.adapters.workflow_definitions import custom_role_names
            from agent_dev.domain.role_policy import instruction

            custom_roles = custom_role_names(resolved)

            node_ids = [n["id"] for n in resolved["workflow_graph"]["nodes"]]
            for node in resolved["workflow_graph"]["nodes"]:
                if node["kind"] == "role":
                    contract = role_context.get(node["id"]) or instruction(
                        node["role_id"], node_ids
                    )
                    built_in = defaults.role_path(node["role_id"], domain)
                    custom = custom_roles.get(node["role_id"])
                    if built_in:
                        body = built_in_text(node["id"], node["role_id"], built_in)
                    elif custom:
                        body = f"# {custom['name']}\n\n{custom['instruction']}"
                    else:
                        body = ""
                    role_context[node["id"]] = f"{body}\n\n{contract}" if body else contract
        for phase in resolved["phases"]:
            role_file = phase.get("role_file")
            if role_file:
                path = defaults.role_path(role_file, domain)
                if path is None:
                    raise DomainError(ErrorCode.INVALID_INPUT, "Unknown built-in role file.")
                role_context[phase["id"]] = built_in_text(phase["id"], role_file, path)
        resolved["role_context"] = role_context
        if role_sources:
            resolved["role_sources"] = role_sources
        resolved["operating_rules"] = defaults.operating_rules(domain)
        # Project rules (.ai/rules) are frozen here, so a later edit only affects new tasks.
        from agent_dev.adapters.ai_rules import snapshot_value

        project_rules = snapshot_value(self.project_root)
        if project_rules["items"]:
            resolved["project_rules"] = project_rules
        from agent_dev.adapters.ai_rules import skills_snapshot

        project_skills = skills_snapshot(self.project_root)
        if project_skills["items"]:
            resolved["project_skills"] = project_skills
        # Hooks (.ai/hooks) run only as approved by this user, exactly as reviewed.
        from agent_dev.adapters import ai_hooks

        project_hooks = ai_hooks.snapshot_value(self.project_root, self.root)
        if project_hooks:
            resolved["project_hooks"] = dict(project_hooks, root=str(self.project_root))
        resolved["domain"] = domain
        knowledge = resolved.get("knowledge", {})
        resolved["knowledge_facts"] = (
            self.governance.search_knowledge(task.project_id, knowledge.get("query", ""))
            if knowledge
            else []
        )
        # Wiki items are selected once here; prompts and retries read only this bundle.
        resolved["wiki"] = self.wiki.bundle(
            task.project_id,
            resolved["workflow_graph"],
            " ".join(
                str(task.definition.get(field, "")) for field in ("title", "description")
            ),
            resolved.get("execution_module"),
        )
        # Approved skills are selected once per role node, like wiki items.
        resolved["approved_skills"] = approved_skills(resolved)
        repositories = {
            name: dict(binding, path=str((self.project_root / binding["path"]).resolve()))
            for name, binding in resolved["repositories"].items()
        }
        resolved["repositories"] = WorktreeManager(
            self.locations.worktree_root, self.safety
        ).prepare(task_id, repositories)
        design = self.store.db.rows(
            "SELECT content FROM design_documents WHERE id=? AND version=?",
            (plan["design_id"], plan["design_version"]),
        )[0]
        resolved["design"] = design["content"]
        resolved["profile_version"] = profiles[0]["version"]
        from agent_dev.adapters.snapshot_tools import bind

        return bind(self.root, resolved)

    def runners(self, task_id, lease=None):
        """Runner candidates for the task's approved AI tools."""
        from agent_dev.adapters.ai_providers import API_PROVIDERS, PROVIDERS, credential_ready
        from agent_dev.adapters.api_runner import ApiRunnerAdapter
        from agent_dev.adapters.environment import process_environment

        task = self.store.task(task_id)
        resolved = self.store.snapshot(task_id)["resolved"]
        process_env = process_environment(self.root, resolved.get("environment_keys", []))
        if resolved.get("ai_settings_origin"):
            # API keys saved on the AI tools screen fill only keys the project does not set.
            from agent_dev.adapters.global_ai import shared_keys

            wanted = {
                item.get("config", {}).get("credential_ref", "")
                for item in resolved.get("runners", [])
                if item.get("provider") in API_PROVIDERS
            }
            process_env = {
                **{k: v for k, v in shared_keys().items() if k in wanted},
                **process_env,
            }
        execution_module = resolved.get("execution_module")
        workspace = Path(
            (
                resolved["repositories"][execution_module]
                if execution_module
                else next(iter(resolved["repositories"].values()))
            )["worktree"]
        )
        candidates = []
        for item in resolved.get("runners", []):
            if item.get("enabled") is not True:
                continue
            provider = item["provider"]
            binary = PROVIDERS.get(provider, {}).get("command", "")
            binary = item.get("config", {}).get("command", binary)
            connection = ToolConnection(
                item["id"],
                task.project_id,
                frozenset(item.get("capabilities", [])),
                item.get("enabled") is True,
                item.get("authenticated") is True
                and credential_ready(provider, item.get("config", {}), process_env)
                if provider in API_PROVIDERS
                else item.get("authenticated") is True,
                provider in API_PROVIDERS or bool(binary and shutil.which(binary)),
            )
            candidates.append(
                RunnerCandidate(
                    connection,
                    ApiRunnerAdapter(
                        provider, dict(item.get("config", {}), process_env=process_env)
                    )
                    if provider in API_PROVIDERS
                    else CliRunnerAdapter(
                        provider,
                        dict(
                            item.get("config", {}),
                            process_env=process_env,
                            approved_add_dirs=[
                                binding["worktree"]
                                for binding in resolved["repositories"].values()
                                if not execution_module and Path(binding["worktree"]) != workspace
                            ],
                            skill_dirs=[
                                item["path"]
                                for item in resolved.get("approved_skills", {})
                                .get("items", {})
                                .values()
                            ],
                            project_checks=project_checks(resolved) if provider == "claude_cli" else [],
                        ),
                        workspace,
                        dict(lease, db=str(self.store.db.path)) if lease else None,
                    ),
                )
            )
        return CapabilityRunnerRegistry(candidates, self.safety)

    def execute(self, task_id, request_id, *, verify=False, runner=None, tools=None):
        from agent_dev.adapters import workspace_trust

        # Snapshots in a database that came with the folder were approved by someone else.
        workspace_trust.require(self.root, "검증" if verify else "작업 실행")
        if not verify:
            from agent_dev.adapters.preparation import require_ai

            require_ai(self.root)
        self.store.assert_change_allowed(task_id)
        self.require_flag("execute")

        def operation():
            resolved = self.validate_snapshot_paths(task_id)
            with ExitStack() as stack:
                lease = stack.enter_context(self.safety.lease(f"task:{task_id}"))
                for common in sorted(
                    {v["common_dir"] for v in resolved.get("repositories", {}).values()}
                ):
                    stack.enter_context(self.safety.lease(f"repository:{common}"))
                if verify:
                    return VerificationAdapter(self.store, self.artifacts, self.root).verify(
                        task_id, lease
                    )
                coordinator = WorkflowCoordinator(
                    self.store,
                    runner or self.runners(task_id, lease),
                    self.artifacts,
                    SnapshotPhaseCatalog(self.store, self.root),
                    parse_harness,
                    tools=tools,
                    hooks=self.task_hooks,
                )
                return coordinator.execute(task_id, lease)

        return self.safety.command(request_id, {"task_id": task_id, "verify": verify}, operation)

    def task_hooks(self, task_id, event, context):
        """Run a task's approved project hooks for one moment; [] when it has none."""
        from agent_dev.adapters import ai_hooks

        resolved = self.store.snapshot(task_id)["resolved"]
        bundle = resolved.get("project_hooks")
        if not bundle:
            return []
        bindings = list(resolved.get("repositories", {}).values())
        cwd = bindings[0]["worktree"] if bindings else bundle["root"]
        results = ai_hooks.run(
            event, bundle, bundle["root"], cwd, dict(context, task_id=task_id), self.store.secrets
        )
        if results:
            self.store.event(task_id, "HookRun", {"event": event, "results": results})
        return results

    def validate_snapshot_paths(self, task_id):
        resolved = self.store.snapshot(task_id)["resolved"]
        scope = self.locations.worktree_root
        if not all(
            Path(binding["worktree"]).resolve().is_relative_to(scope)
            for binding in resolved.get("repositories", {}).values()
        ):
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "Snapshot belongs to another workspace. Create newly approved tasks after relocation.",
            )
        return resolved
