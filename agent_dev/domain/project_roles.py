"""Project role roster: locked base roles, optional built-ins and user-defined roles.

Stored as ``profile["roles"] = {"domain", "order", "custom"}``. Base roles come
from the bundled AGENTS.md and can be reordered but never removed; optional
built-ins (FMEA Expert) follow the domain profile by default and can be added
or removed; custom roles carry user-written instructions and an explicit
permission mode encoded in their ID so role authority stays a pure function.
"""

import re
from copy import deepcopy
from typing import NoReturn

from agent_dev.domain.errors import DomainError, ErrorCode

# AGENTS.md section 2 order; FMEA Expert follows Planner so methodology decisions
# feed Architect/DBA design (ADR 0002).
CATALOG = (
    "planner",
    "fmea-expert",
    "architect",
    "dba",
    "ux-ui",
    "designer",
    "developer",
    "developer-frontend",
    "developer-backend",
    "developer-infra",
    "qa",
    "test-automation",
    "security",
    "reviewer",
    "tech-writer",
)
OPTIONAL = frozenset({"fmea-expert"})
BASE = tuple(role for role in CATALOG if role not in OPTIONAL)
DOMAINS = {"generic": (), "fmea": ("fmea-expert",)}
LABELS = {
    "planner": "Planner",
    "architect": "Architect",
    "dba": "DBA",
    "ux-ui": "UX/UI",
    "designer": "Designer",
    "fmea-expert": "FMEA Expert",
    "developer": "Developer Common",
    "developer-frontend": "Frontend Developer",
    "developer-backend": "Backend Developer",
    "developer-infra": "Infra Developer",
    "qa": "QA",
    "test-automation": "Test Automation Developer",
    "security": "Security Reviewer",
    "reviewer": "Reviewer",
    "tech-writer": "Tech Writer",
}
# ADR 0002 sessions: consecutive roles of one session share a single runner invocation.
SESSIONS = ("analysis", "design", "develop", "verify", "docs")
ROLE_SESSIONS = {
    "planner": "analysis",
    "fmea-expert": "analysis",
    "architect": "analysis",
    "dba": "analysis",
    "ux-ui": "design",
    "designer": "design",
    "developer": "develop",
    "developer-frontend": "develop",
    "developer-backend": "develop",
    "developer-infra": "develop",
    "qa": "verify",
    "test-automation": "verify",
    "security": "verify",
    "reviewer": "verify",
    # Separate session so a graph tool node may run between verification and documents.
    "tech-writer": "docs",
}
DEFAULT_GATES = {"analysis": True}
# Integration connection id the Tech Writer queries for the wiki codebase map (D4).
GRAPH_CONNECTION = "graphify"
CUSTOM_MODES = ("readonly", "documents", "code")
CUSTOM_ID = re.compile(r"^custom-(readonly|documents|code)-[a-z0-9][a-z0-9-]{0,39}$")
MAX_CUSTOM = 20


def invalid(message) -> NoReturn:
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def is_custom(role):
    return isinstance(role, str) and CUSTOM_ID.fullmatch(role) is not None


def custom_mode(role):
    match = CUSTOM_ID.fullmatch(role) if isinstance(role, str) else None
    return match.group(1) if match else ""


def default_roles(domain="generic"):
    if domain not in DOMAINS:
        invalid("알 수 없는 도메인 프로필입니다.")
    enabled = set(BASE) | set(DOMAINS[domain])
    return {"domain": domain, "order": [r for r in CATALOG if r in enabled], "custom": []}


def from_graph(graph):
    """Roster view for profiles saved before rosters existed (keeps graph order)."""
    roles = default_roles()
    nodes = [n["role_id"] for n in (graph or {}).get("nodes", []) if n.get("kind") == "role"]
    present = [r for r in dict.fromkeys(nodes) if r in CATALOG]
    if "fmea-expert" in present:
        roles["domain"] = "fmea"
    order = list(present)
    for index, role in enumerate(CATALOG):
        if role in BASE and role not in order:
            before = next((r for r in CATALOG[index + 1 :] if r in order), None)
            order.insert(order.index(before) if before else len(order), role)
    roles["order"] = order
    return roles


def view(profile):
    stored = (profile or {}).get("roles")
    if stored:
        return normalize(stored)
    return from_graph(((profile or {}).get("workflow") or {}).get("document"))


def sessions(roles):
    """Role -> session for a roster; custom code roles develop, others join the previous role.

    A session that reappears after another one (a reordered roster) gets a numbered
    id so every session stays one contiguous span.
    """
    result, previous, base, used = {}, None, None, {}
    for role in roles["order"]:
        if role in ROLE_SESSIONS:
            name = ROLE_SESSIONS[role]
        elif custom_mode(role) == "code":
            name = "develop"
        else:
            name = base
        if name and name != base:
            used[name] = used.get(name, 0) + 1
            previous = name if used[name] == 1 else f"{name}-{used[name]}"
        if name:
            result[role] = previous
        base = name
    return result


def gates(roles):
    """Sessions followed by an approval node when the roster runs in session mode."""
    value = roles.get("gates", DEFAULT_GATES)
    return [session for session in SESSIONS if value.get(session)]


def normalize(value):
    if not isinstance(value, dict) or set(value) - {
        "domain",
        "order",
        "custom",
        "session_mode",
        "gates",
    }:
        invalid("역할 설정 형식이 올바르지 않습니다.")
    if type(value.get("session_mode", False)) is not bool:
        invalid("세션 실행 설정이 올바르지 않습니다.")
    if "gates" in value and (
        not isinstance(value["gates"], dict)
        or set(value["gates"]) - set(SESSIONS)
        or any(type(v) is not bool for v in value["gates"].values())
    ):
        invalid("세션 승인 설정이 올바르지 않습니다.")
    domain = value.get("domain", "generic")
    if domain not in DOMAINS:
        invalid("알 수 없는 도메인 프로필입니다.")
    custom = value.get("custom", [])
    if not isinstance(custom, list) or len(custom) > MAX_CUSTOM:
        invalid(f"추가 역할은 최대 {MAX_CUSTOM}개입니다.")
    rows = []
    for row in custom:
        if not isinstance(row, dict) or set(row) - {"id", "name", "mode", "instruction"}:
            invalid("추가 역할 형식이 올바르지 않습니다.")
        name = row.get("name")
        instruction = row.get("instruction")
        mode = row.get("mode")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            invalid("추가 역할 이름은 1–60자로 입력해주세요.")
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 8000:
            invalid(f"'{name}' 역할 지시문은 1–8000자로 입력해주세요.")
        if mode not in CUSTOM_MODES:
            invalid(f"'{name}' 역할 권한을 선택해주세요.")
        if not is_custom(row.get("id")) or custom_mode(row["id"]) != mode:
            invalid(f"'{name}' 역할 ID가 권한과 맞지 않습니다.")
        rows.append(
            {"id": row["id"], "name": name.strip(), "mode": mode, "instruction": instruction.strip()}
        )
    custom_ids = [row["id"] for row in rows]
    if len(set(custom_ids)) != len(custom_ids):
        invalid("추가 역할 ID가 중복되었습니다.")
    order = value.get("order")
    if not isinstance(order, list) or len(set(order)) != len(order):
        invalid("역할 순서에 중복이 있습니다.")
    known = set(CATALOG) | set(custom_ids)
    unknown = [r for r in order if r not in known]
    if unknown:
        invalid("등록되지 않은 역할입니다: " + ", ".join(map(str, unknown)))
    missing = [LABELS[r] for r in BASE if r not in order]
    if missing:
        invalid("기본 역할은 삭제할 수 없습니다: " + ", ".join(missing))
    unordered = [row["name"] for row in rows if row["id"] not in order]
    if unordered:
        invalid("추가 역할을 순서에 포함해주세요: " + ", ".join(unordered))
    result = {"domain": domain, "order": list(order), "custom": rows}
    # Optional keys stay absent unless set so existing rosters compare equal.
    if value.get("session_mode"):
        result["session_mode"] = True
    if "gates" in value:
        result["gates"] = dict(value["gates"])
    return result


def catalog(roles=None):
    """Role list for the settings screen."""
    roles = deepcopy(roles) if roles else default_roles()
    names = {row["id"]: row["name"] for row in roles.get("custom", [])}
    return [
        {
            "id": role,
            "label": LABELS.get(role) or names.get(role, role),
            "kind": "base" if role in BASE else "optional" if role in OPTIONAL else "custom",
            "locked": role in BASE,
            "session": ROLE_SESSIONS.get(role, ""),
        }
        for role in [*CATALOG, *names]
    ]
