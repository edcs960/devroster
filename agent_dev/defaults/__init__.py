"""Built-in operating rules, roles, phase guides, skill and default pipeline config.

These files ship inside the package and are the only rule source: customer
project folders are never read for rules, so every installation runs the same
instructions. Repository-root copies used while developing this project are
generated from here by ``scripts/sync_defaults.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_dev.core.io import read_text

DEFAULTS_DIR = Path(__file__).resolve().parent
CONFIG_DEFAULTS_DIR = DEFAULTS_DIR / "config"

# (prompt label, bundled path) — labels keep the familiar repository paths.
# SKILL.md and prompts/feature-dev.md are interactive entry documents that
# restate AGENTS.md and the role files, so phase prompts leave them out.
GLOBAL_RULE_FILES = (
    ("AGENTS.md", "AGENTS.md"),
    (
        ".agents/skills/dev-pipeline/handoff-checkpoint.md",
        "skills/dev-pipeline/handoff-checkpoint.md",
    ),
)

# AGENTS.md parts that only matter to a person driving an interactive session:
# request-mode classification (4), how to invoke the skill (10) and the local
# tool inventory upkeep (7.1). Design reference rules (6) go to design roles.
PHASE_EXCLUDED_SECTIONS = ("4", "10", "7.1")
DESIGN_SECTION = "6"
DESIGN_AGENT_IDS = frozenset(
    {"planner", "architect", "ux_ui", "designer", "developer_frontend", "qa", "reviewer"}
)
_SECTION = re.compile(r"^(#{2,3}) (\d+(?:\.\d+)?)\.? ")

# Bundled path prefix -> repository-root prefix for scripts/sync_defaults.py.
ROOT_SYNC_MAP = (
    ("AGENTS.md", "AGENTS.md"),
    ("CLAUDE.md", "CLAUDE.md"),
    ("roles/", "agent/"),
    ("skills/", ".agents/skills/"),
    ("prompts/", "prompts/"),
)

ROLE_ALIASES = {"design": "designer"}


def role_name(role_file: str) -> str:
    stem = Path(role_file).stem
    return ROLE_ALIASES.get(stem, stem)


# Domain packs: a project's domain may replace bundled roles, phase guides and
# prompts with its own copies and append its own section to AGENTS.md. Everything else
# stays generic, so a project without a domain never receives another domain's rules.
DOMAINS_DIR = DEFAULTS_DIR / "domains"
GENERIC = "generic"


def domain_name(value) -> str:
    """A bundled domain pack name, or "generic"."""
    if (
        isinstance(value, str)
        and re.fullmatch(r"[a-z][a-z0-9-]{0,40}", value)
        and (DOMAINS_DIR / value).is_dir()
    ):
        return value
    return GENERIC


def bundled_path(relative: str, domain: str | None = None) -> Path:
    """The domain pack's copy of a bundled file when it has one, else the generic file."""
    name = domain_name(domain)
    if name != GENERIC:
        candidate = DOMAINS_DIR / name / relative
        if candidate.is_file():
            return candidate
    return DEFAULTS_DIR / relative


def agents_text(domain: str | None = None) -> str:
    """AGENTS.md followed by the domain pack's own rules section, if any."""
    text = (DEFAULTS_DIR / "AGENTS.md").read_text(encoding="utf-8")
    name = domain_name(domain)
    extra = DOMAINS_DIR / name / "AGENTS.md"
    if name != GENERIC and extra.is_file():
        text = text.rstrip("\n") + "\n\n---\n\n" + extra.read_text(encoding="utf-8")
    return text


def role_path(role_file: str, domain: str | None = None) -> Path | None:
    if not role_file:
        return None
    path = bundled_path(f"roles/{role_name(role_file)}.md", domain)
    return path if path.is_file() else None


def role_text(role_file: str, limit_chars: int | None = None, domain: str | None = None) -> str:
    path = role_path(role_file, domain)
    if path is None:
        return f"[missing built-in role: {role_file}]"
    return read_text(path, limit_chars=limit_chars)


def select_sections(text: str, excluded) -> str:
    """Drop numbered `##`/`###` sections (with their subsections) by number."""
    excluded = set(excluded)
    kept, skip_level, fenced = [], 0, False
    for line in text.split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        heading = None if fenced else re.match(r"^(#{2,3}) ", line)
        if heading:
            level = len(heading.group(1))
            if not (skip_level and level > skip_level):
                match = _SECTION.match(line)
                skip_level = level if match and match.group(2) in excluded else 0
        if not skip_level:
            kept.append(line)
    return "\n".join(kept)


def phase_rules_text(text: str, agent_id: str | None = None) -> str:
    excluded = list(PHASE_EXCLUDED_SECTIONS)
    if agent_id is not None and agent_id not in DESIGN_AGENT_IDS:
        excluded.append(DESIGN_SECTION)
    return select_sections(text, excluded)


def global_rules(
    agent_id: str | None = None, domain: str | None = None
) -> list[tuple[str, str]]:
    """Phase-prompt rules, never truncated (bundled and size-controlled)."""
    rows = []
    for label, relative in GLOBAL_RULE_FILES:
        text = (
            agents_text(domain)
            if label == "AGENTS.md"
            else (DEFAULTS_DIR / relative).read_text(encoding="utf-8")
        )
        rows.append((label, phase_rules_text(text, agent_id) if label == "AGENTS.md" else text))
    return rows


def phase_guides(
    step_id: str, limit_chars: int | None = None, domain: str | None = None
) -> list[tuple[str, str]]:
    names = ["README.md", f"{step_id}.md"]
    paths = [(name, bundled_path(f"phases/{name}", domain)) for name in names]
    return [
        (f"phases/{name}", read_text(path, limit_chars=limit_chars))
        for name, path in paths
        if path.is_file()
    ]


# Snapshot-bound (v2) contexts also drop the role file list (each node carries its own role
# instruction) and the agent handoff rules (DevRoster records checkpoints).
V2_EXCLUDED_SECTIONS = ("1", "11")


def operating_rules(domain: str | None = None) -> str:
    """Global rules as one text block for snapshot-bound (v2) phase contexts."""
    return "\n\n".join(
        f"# `{label}`\n\n"
        + (select_sections(text, V2_EXCLUDED_SECTIONS) if label == "AGENTS.md" else text)
        for label, text in global_rules(domain=domain)
    )


def config_file(name: str) -> Path:
    return CONFIG_DEFAULTS_DIR / name


def workspace_or_default(local_base: Path, relative: str | Path, default_base: Path) -> Path:
    """Workspace path when present, otherwise the bundled default (profiles, templates)."""
    relative = Path(relative)
    if relative.is_absolute():
        return relative
    local = Path(local_base) / relative
    return local if local.exists() else Path(default_base) / relative
