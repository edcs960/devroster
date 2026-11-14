"""Import direction between layers (.ai/rules/project.md) and private-name boundaries.

Every module under agent_dev belongs to one layer. A layer may import itself and the
layers listed for it in ALLOWED; everything else is a violation. The violations that
existed when this check was added are listed in KNOWN_*; the lists may only shrink.
Fixing one means deleting its entry here, and the test fails until that happens so the
lists never go stale.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "agent_dev"

# Top-level package or module under agent_dev -> layer.
LAYERS = {
    "domain": "domain",
    "application": "application",
    "ports": "application",
    "adapters": "adapters",
    "core": "adapters",
    "runners": "adapters",
    "scm": "adapters",
    "storage": "adapters",
    "migration": "adapters",
    "config": "adapters",
    "defaults": "adapters",
    "templates": "adapters",
    "ui": "ui",
    "app": "bootstrap",
    "entrypoints": "bootstrap",
    "fa": "bootstrap",
    "__main__": "bootstrap",
}
# The package root only carries __version__; any layer may read it.
ROOT = "agent_dev"
ALLOWED = {
    "domain": {"domain"},
    "application": {"application", "domain"},
    "adapters": {"adapters", "application", "domain"},
    "ui": {"ui", "adapters", "application", "domain"},
    "bootstrap": {"bootstrap", "ui", "adapters", "application", "domain"},
}

# (importing module, imported module) -> why it is still here. The six found by the
# architecture audit are fixed; keep it empty.
KNOWN_LAYER_EXCEPTIONS: dict[tuple[str, str], str] = {}
# (importing module, imported module, private name).
KNOWN_PRIVATE_IMPORTS = {
    ("agent_dev.adapters.development_review", "agent_dev.adapters.traceability", "_artifact_report"),
    ("agent_dev.adapters.requirement_changes", "agent_dev.adapters.traceability", "_requirement_origin"),
    ("agent_dev.adapters.live_log", "agent_dev.adapters.role_execution", "_attempts_dir"),
    ("agent_dev.adapters.storage_migration", "agent_dev.adapters.backup", "_quiescent"),
}
# Groups of modules that import each other in a circle, in-function imports included:
# (package prefix, module names). A module leaves its group when every circle through it
# is cut; no module may join and two groups may not merge.
KNOWN_CYCLES: list[tuple[str, str]] = [
    (
        "agent_dev.adapters.",
        (
            "ai_hooks ai_providers api_runner artifacts cli_runner code_index credential_store "
            "design_references development_review diagnostics drift_check environment execution_view "
            "global_ai global_settings governance integration_oauth integration_store integrations "
            "phase_catalog preparation preview_host project_setup requirement_changes secrets "
            "shared_integrations snapshot_tools source_explorer source_jobs source_search "
            "state.sqlite.database state.sqlite.safety state.sqlite.store studio_generation "
            "studio_handoff task_queries traceability verification wiki_knowledge workflow_definitions "
            "workflow_runtime workspace workspace_registry workspace_trust"
        ),
    ),
]


def module_name(path):
    parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def is_module(name):
    relative = Path(*name.split(".")[1:]) if "." in name else Path()
    return (PACKAGE / relative).with_suffix(".py").is_file() or (PACKAGE / relative / "__init__.py").is_file()


def layer(name):
    if name == ROOT:
        return "root"
    return LAYERS.get(name.split(".")[1])


def imports():
    """(importer, imported module, imported names) for every agent_dev import, lazy ones included."""
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        importer = module_name(path)
        package = importer if path.name == "__init__.py" else importer.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            if isinstance(node, ast.Import):
                found += [(importer, a.name, ()) for a in node.names if a.name.split(".")[0] == ROOT]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package.split(".")
                    base = base[: len(base) - node.level + 1]
                    target = ".".join([*base, node.module] if node.module else base)
                else:
                    target = node.module or ""
                if target.split(".")[0] != ROOT:
                    continue
                names = tuple(a.name for a in node.names)
                # "from agent_dev import defaults" imports the submodule, not a root attribute.
                submodules = [f"{target}.{n}" for n in names if is_module(f"{target}.{n}")]
                found += [(importer, m, ()) for m in submodules] or [(importer, target, names)]
                if submodules and len(submodules) < len(names):
                    found.append((importer, target, tuple(n for n in names if not is_module(f"{target}.{n}"))))
            elif (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.split(".")[0] == ROOT
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "__import__")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module")
                )
            ):
                found.append((importer, node.args[0].value, ()))
    return found


def test_every_package_has_a_layer():
    top = {p.stem for p in PACKAGE.glob("*.py")} | {
        p.name for p in PACKAGE.iterdir() if (p / "__init__.py").is_file()
    }
    unassigned = sorted(top - set(LAYERS) - {"__init__"})
    assert not unassigned, f"Assign a layer in tests/test_architecture.py LAYERS: {unassigned}"


def test_layers_only_import_inward():
    seen = set()
    for importer, target, _names in imports():
        source, destination = layer(importer), layer(target)
        if destination == "root" or destination in ALLOWED[source]:
            continue
        seen.add((importer, target))
    new = sorted(seen - set(KNOWN_LAYER_EXCEPTIONS))
    assert not new, (
        "Imports against the layer direction (.ai/rules/project.md). Move the shared code "
        f"inward or call it through the outer layer: {new}"
    )
    fixed = sorted(set(KNOWN_LAYER_EXCEPTIONS) - seen)
    assert not fixed, f"Fixed; delete these from KNOWN_LAYER_EXCEPTIONS: {fixed}"


def test_no_new_private_imports_across_modules():
    seen = {
        (importer, target, name)
        for importer, target, names in imports()
        for name in names
        if importer != target and name.startswith("_") and not name.startswith("__")
    }
    new = sorted(seen - KNOWN_PRIVATE_IMPORTS)
    assert not new, f"Private names imported from another module; make them public or move them: {new}"
    fixed = sorted(KNOWN_PRIVATE_IMPORTS - seen)
    assert not fixed, f"Fixed; delete these from KNOWN_PRIVATE_IMPORTS: {fixed}"


def test_checker_sees_lazy_relative_and_dynamic_imports():
    """The violations above hide inside functions; the checker must still find them."""
    pairs = {(i, t) for i, t, _ in imports()}
    assert ("agent_dev.adapters.messenger", "agent_dev.adapters.studio_generation") in pairs  # inside a function
    assert ("agent_dev.application.workflow", "agent_dev.application.workflow_dag") in pairs  # relative
    assert ("agent_dev.adapters.task_queries", "agent_dev.adapters.execution_view") in pairs  # __import__
    assert ("agent_dev.adapters.skill_store", "agent_dev.defaults") in pairs  # from agent_dev import x


def test_workspace_location_modules_do_not_import_their_callers():
    """One module decides where a workspace lives; its callers never get imported back.

    ``workspace_locator`` reads the registry (``storage_registry``) and composes paths.
    Before, the same decision was spread over five modules that imported each other
    inside functions (workspace -> storage_registry -> workflow_runtime -> workspace).
    """
    adapters = "agent_dev.adapters."
    callers = {adapters + n for n in ("workspace", "workflow_runtime", "project_setup", "workspace_registry")}
    below = {adapters + "workspace_locator", adapters + "storage_registry"}
    back = sorted((i, t) for i, t, _ in imports() if i in below and t in callers)
    assert not back, f"The locator and the registry must not import their callers: {back}"
    assert (adapters + "storage_registry", adapters + "workspace_locator") not in {(i, t) for i, t, _ in imports()}


def strongly_connected(pairs):
    """Groups of two or more modules that can all reach each other along ``pairs``."""
    forward, backward = {}, {}
    for source, target in pairs:
        if source != target:
            forward.setdefault(source, set()).add(target)
            backward.setdefault(target, set()).add(source)

    def walk(start, edges, seen):
        order, stack = [], [(start, iter(sorted(edges.get(start, ()))))]
        seen.add(start)
        while stack:
            rest = stack[-1][1]
            following = next((n for n in rest if n not in seen), None)
            if following is None:
                order.append(stack.pop()[0])
            else:
                seen.add(following)
                stack.append((following, iter(sorted(edges.get(following, ())))))
        return order

    seen, finished = set(), []
    for node in sorted(forward):
        if node not in seen:
            finished += walk(node, forward, seen)
    seen, groups = set(), []
    for node in reversed(finished):
        if node not in seen:
            group = set(walk(node, backward, seen))
            if len(group) > 1:
                groups.append(group)
    return groups


def import_cycles():
    return strongly_connected((i, t) for i, t, _ in imports() if t != ROOT)


def test_cycle_finder_groups_only_modules_that_reach_each_other():
    groups = strongly_connected([("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("e", "f"), ("f", "e"), ("g", "g")])
    assert sorted(sorted(g) for g in groups) == [["a", "b", "c"], ["e", "f"]]
    assert strongly_connected([("a", "b"), ("b", "c")]) == []


def test_import_cycles_only_shrink():
    """An import inside a function hides a circle from Python, not from the reader.

    The circles that existed when this check was added are in KNOWN_CYCLES. Cut one by
    moving the shared part into a module below both sides, as ``windows_job`` did for the
    process modules, then delete the names that left.
    """
    known = [{prefix + name for name in names.split()} for prefix, names in KNOWN_CYCLES]
    found = import_cycles()
    new = sorted(sorted(group - next((k for k in known if group & k), set())) for group in found)
    new = [group for group in new if group]
    assert not new, f"New import circle, or modules joining a known one: {new}"
    merged = [sorted(group) for group in found if sum(bool(group & k) for k in known) > 1]
    assert not merged, f"Known import circles merged into one: {merged}"
    inside = set().union(*found) if found else set()
    fixed = sorted(set().union(*known) - inside) if known else []
    assert not fixed, f"Fixed; delete these from KNOWN_CYCLES: {fixed}"
