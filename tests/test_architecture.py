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
    ("agent_dev.adapters.stdio_transport", "agent_dev.adapters.bounded_process", "_exited_unreaped"),
    ("agent_dev.adapters.stdio_transport", "agent_dev.adapters.bounded_process", "_signal_owned_group"),
    ("agent_dev.adapters.stdio_transport", "agent_dev.adapters.bounded_process", "_wait_options"),
}


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
