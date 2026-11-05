"""Copy the bundled rules (agent_dev/defaults) to the repository-root development copies.

The package is the single source of truth. Run after editing a bundled rule:

    python scripts/sync_defaults.py [--root <repository root>] [--check] [--domain fmea]

--check only reports drift (exit 1 when any root copy differs or is missing).
--domain composes a domain pack (its role/prompt copies and its AGENTS.md section). The
default is the engine's own project domain for the development workspace, else generic.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_dev.defaults import DEFAULTS_DIR, ROOT_SYNC_MAP, agents_text, bundled_path, domain_name

DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def default_domain(root: Path) -> str:
    """The engine's configured project domain for its development workspace, else generic."""
    if root.resolve() != DEFAULT_ROOT.resolve():
        return "generic"
    import json

    runtime = Path(__file__).resolve().parents[1] / "config/runtime.json"
    try:
        return domain_name(json.loads(runtime.read_text(encoding="utf-8")).get("project_profile"))
    except (OSError, ValueError, AttributeError):
        return "generic"


def pairs(root: Path, domain: str | None = None) -> list[tuple[Path, Path]]:
    """(bundled source, root target); the source is the domain pack's copy when it has one."""
    domain = default_domain(root) if domain is None else domain
    result = []
    for source_prefix, root_prefix in ROOT_SYNC_MAP:
        source = DEFAULTS_DIR / source_prefix
        if source.is_file():
            result.append((source, root / root_prefix))
            continue
        for path in sorted(source.rglob("*.md")):
            relative = path.relative_to(DEFAULTS_DIR).as_posix()
            result.append((bundled_path(relative, domain), root / root_prefix / path.relative_to(source)))
    return result


def _content(source: Path, domain: str) -> bytes:
    if source == DEFAULTS_DIR / "AGENTS.md":
        return agents_text(domain).encode("utf-8")
    return source.read_bytes()


# Tool entry files where installed tools (e.g. graphify) append their own "## " sections.
# The root copy must start with the bundled text; appended sections are preserved.
APPENDABLE = {"CLAUDE.md"}


def _appendable(source: Path) -> bool:
    return source.name in APPENDABLE and source.parent == DEFAULTS_DIR


def _differs(source: Path, target: Path, domain: str = "generic") -> bool:
    if not target.is_file():
        return True
    content = _content(source, domain)
    if _appendable(source):
        return not target.read_bytes().startswith(content)
    return target.read_bytes() != content


def _synced(source: Path, target: Path, domain: str = "generic") -> bytes:
    content = _content(source, domain)
    if not (_appendable(source) and target.is_file()):
        return content
    old = target.read_text(encoding="utf-8")
    start = next((i for i, line in enumerate(old.splitlines()) if line.startswith("## ")), None)
    if start is None:
        return content
    tail = "\n".join(old.splitlines()[start:]) + "\n"
    return (content.decode("utf-8").rstrip("\n") + "\n\n" + tail).encode("utf-8")


def drift(root: Path, domain: str | None = None) -> list[Path]:
    domain = default_domain(root) if domain is None else domain
    return [target for source, target in pairs(root, domain) if _differs(source, target, domain)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--domain", default=None, help="domain pack to compose (default: auto)")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    domain = default_domain(root) if args.domain is None else domain_name(args.domain)
    changed = drift(root, domain)
    if args.check:
        for target in changed:
            print(f"drift: {target.relative_to(root)}")
        return 1 if changed else 0
    for source, target in pairs(root, domain):
        if target in changed:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_synced(source, target, domain))
            print(f"synced: {target.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
