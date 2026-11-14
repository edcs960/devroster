"""Shared role authority. Role names never grant additional filesystem scope."""

from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.project_roles import custom_mode, is_custom

READ_ONLY = frozenset(
    {
        "planner",
        "architect",
        "dba",
        "ux-ui",
        "designer",
        "fmea-expert",
        "qa",
        "security",
        "reviewer",
    }
)
DEVELOPERS = frozenset({"developer", "developer-frontend", "developer-backend", "developer-infra"})
ROLES = READ_ONLY | DEVELOPERS | {"test-automation", "tech-writer"}
ALIASES = {
    "design": "designer",
    "frontend": "developer-frontend",
    "backend": "developer-backend",
    "infra": "developer-infra",
    "security-reviewer": "security",
}


def role_id(value) -> str:
    value = ALIASES.get(value, value)
    if not isinstance(value, str) or not (is_custom(value) or value in ROLES):
        raise DomainError(ErrorCode.INVALID_INPUT, "Unknown role_id: " + str(value))
    return value


def policy(
    value, *, project_paths=(), module_paths=(), node_paths=(), test_paths=(), document_paths=()
):
    role = role_id(value)
    scopes = [list(project_paths)]
    if module_paths:
        scopes.append(list(module_paths))
    if node_paths:
        scopes.append(list(node_paths))
    custom = custom_mode(role)
    if custom:
        mode = {"readonly": "readonly", "documents": "structured", "code": "code"}[custom]
    else:
        mode = "readonly" if role in READ_ONLY else "code" if role in DEVELOPERS else "structured"
    documents = role == "tech-writer" or custom == "documents"
    if role == "test-automation":
        scopes.append(list(test_paths))
    if documents:
        scopes.append(list(document_paths))
    return {
        "role_id": role,
        "mode": mode,
        "scopes": scopes,
        "capabilities": ["code.read", "code.write"] if mode == "code" else ["code.read"],
        "structured_kind": "tests"
        if role == "test-automation"
        else "documents"
        if documents
        else "",
    }


def session_policy(policies):
    """Direct-write policy for one invocation serving several nodes: a path passes when any
    member code policy allows it; sessions without code members stay read-only."""
    code = [p for p in policies if p.get("mode") == "code"]
    if not code:
        return {
            "role_id": "",
            "mode": "readonly",
            "scopes": [],
            "capabilities": ["code.read"],
            "structured_kind": "",
        }
    return {
        "role_id": "",
        "mode": "code",
        "any_of": code,
        "scopes": [],
        "capabilities": ["code.read", "code.write"],
        "structured_kind": "",
    }


def path_refusal(root, relative, role_policy):
    """Why the role may not change ``relative``; None when it may.

    "unsafe": a path nobody may change (.git, symlink, outside the repository).
    "readonly": the role cannot change files directly. "documents": a document role outside
    the document folders. "scope": outside the approved path scopes.
    """
    if "any_of" in role_policy:
        reasons = []
        for member in role_policy["any_of"]:
            reason = path_refusal(root, relative, member)
            if reason is None:
                return None
            reasons.append(reason)
        return "scope" if "scope" in reasons or not reasons else reasons[0]
    if not isinstance(relative, str) or not relative or "\\" in relative:
        return "unsafe"
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
        return "unsafe"
    registered_root = Path(root).resolve()
    path = registered_root
    for component in candidate.parts:
        path = path / component
        if path.is_symlink():
            return "unsafe"
    path = path.resolve()
    if not path.is_relative_to(registered_root):
        return "unsafe"
    if role_policy["mode"] == "readonly":
        return "readonly"
    if role_policy.get("structured_kind") == "documents" and not (
        candidate.parts[0] in {"docs", "deliverables", "output"}
        or candidate.name.startswith("README")
    ):
        return "documents"
    if all(any(path_matches(relative, p) for p in group) for group in role_policy["scopes"]):
        return None
    return "scope"


def allowed_path(root, relative, role_policy):
    return path_refusal(root, relative, role_policy) is None


def path_matches(relative, pattern):
    """A scope entry is a folder/file prefix, or a file-name pattern such as `*.test.*`.

    Patterns cover tests kept next to their source (`src/App.test.jsx`); a
    pattern without `/` matches the file name only, so it never widens to a folder.
    """
    if any(c in pattern for c in "*?["):
        target = relative if "/" in pattern else PurePosixPath(relative).name
        return fnmatchcase(target, pattern)
    prefix = pattern.rstrip("/")
    return relative == prefix or relative.startswith(prefix + "/")


MAX_LISTED = 5  # File names come from the worktree: bound what an error message repeats.


def listed(values, limit=MAX_LISTED, sort=True):
    """Up to ``limit`` names for an error message, without control characters."""
    names = ["".join(c if c.isprintable() else "?" for c in str(value))[:120] for value in values]
    if sort:  # Changed paths arrive in set order; configured scopes keep the order they were given.
        names.sort()
    more = len(names) - limit
    return ", ".join(names[:limit]) + (f" 외 {more}개" if more > 0 else "")


def _covers(wide, narrow):
    """Every path ``narrow`` allows is also allowed by ``wide`` (prefix entries only)."""
    plain = [entry for entry in wide if not any(c in entry for c in "*?[")]
    return all(
        entry in wide
        or (not any(c in entry for c in "*?[") and any(path_matches(entry, p) for p in plain))
        for entry in narrow
    )


def scope_text(role_policy):
    """The approved path scopes in words. A path must fit every remaining group."""
    members = role_policy.get("any_of") or [role_policy]
    texts = []
    for member in members:
        kept = []
        for group in member.get("scopes", []):
            if any(_covers(group, other) for other in kept):
                continue  # A wider group adds nothing to a narrower one already kept.
            kept = [other for other in kept if not _covers(other, group)] + [list(group)]
        if not kept:
            text = "제한 없음"
        elif any(not group for group in kept):
            text = "없음(허용 경로가 비어 있습니다)"
        elif len(kept) == 1:
            text = listed(kept[0], 8, sort=False)
        else:
            text = "다음 범위에 모두 드는 경로: " + " / ".join(
                "(" + listed(group, 8, sort=False) + ")" for group in kept
            )
        if text not in texts:
            texts.append(text)
    return " 또는 ".join(texts)


REVERTS = "이어서 실행하면 {what} 작업 폴더의 .attempts에 복사해 두고 되돌린 뒤 이 단계를 다시 실행합니다."


def refusal_message(refused, role_policy, proposed=False, reverts=False):
    """``refused`` is {reason: [paths]} from path_refusal.

    ``proposed``: nothing was written. ``reverts``: resuming puts the files back by itself.
    """
    did = "바꾸려 했습니다" if proposed else "바꿨습니다"
    parts = []
    if "scope" in refused:
        parts.append(
            f"수정 허용 경로 밖의 파일을 {did}: {listed(refused['scope'])}. "
            f"이 단계의 수정 허용 경로: {scope_text(role_policy)}."
        )
    if "documents" in refused:
        parts.append(
            "문서 역할은 docs, deliverables, output 폴더와 README 파일만 바꿀 수 있는데 "
            f"다른 파일을 {did}: {listed(refused['documents'])}."
        )
    if "readonly" in refused:
        parts.append(
            f"파일을 직접 바꿀 수 없는 역할이 파일을 {did}: {listed(refused['readonly'])}."
        )
    if "unsafe" in refused:
        parts.append(
            "누구도 바꿀 수 없는 경로(.git, 심볼릭 링크, 저장소 밖을 가리키는 경로)를 "
            f"{did}: {listed(refused['unsafe'])}."
        )
    if "scope" in refused:
        parts.append(
            "이 파일을 바꿔야 하면 프로젝트 설정의 모듈 '수정 허용 경로'(단계에서 좁혔다면 "
            "개발 흐름의 '허용 경로')에 추가한 뒤 계획을 새로 승인하세요. "
            "이미 승인된 작업의 범위는 바뀌지 않습니다."
        )
    if proposed:
        parts.append("파일에 적용된 것은 없습니다. 이어서 실행하면 이 단계를 다시 실행합니다.")
    elif reverts:
        parts.append(REVERTS.format(what="위 변경을"))
    elif "scope" in refused:
        parts.append("바꾸면 안 되는 파일이면 작업 폴더에서 그 변경을 되돌린 뒤 이어서 실행하세요.")
    else:
        parts.append("작업 폴더에서 그 변경을 되돌린 뒤 이어서 실행하세요.")
    return " ".join(parts)


def validate_changes(root, paths, role_policy, proposed=False, reverts=False):
    refused = {}
    for path in paths:
        reason = path_refusal(root, path, role_policy)
        if reason:
            refused.setdefault(reason, []).append(path)
    if refused:
        raise DomainError(
            ErrorCode.POLICY_VIOLATION, refusal_message(refused, role_policy, proposed, reverts)
        )


def apply_structured(root, proposals, role_policy):
    """Only bounded text replacements, validated as a complete batch before any write."""
    if (
        role_policy.get("mode") != "structured"
        or not isinstance(proposals, list)
        or len(proposals) > 60
    ):
        raise DomainError(ErrorCode.POLICY_VIOLATION, "Structured narrow-role edits required.")
    prepared = []
    seen = set()
    for edit in proposals:
        if not isinstance(edit, dict) or set(edit) != {"path", "content"} or edit["path"] in seen:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid or duplicate structured edit.")
        validate_changes(root, [edit["path"]], role_policy, proposed=True)
        if not isinstance(edit["content"], str) or len(edit["content"].encode()) > 1_000_000:
            raise DomainError(ErrorCode.INVALID_INPUT, "Structured text exceeds limit.")
        seen.add(edit["path"])
        prepared.append((Path(root) / edit["path"], edit["content"]))
    for path, content in prepared:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return [str(path.relative_to(root)) for path, _ in prepared]


def independent_family(provider):
    if provider.startswith(("codex", "openai")):
        return "openai"
    if provider.startswith(("claude", "anthropic")):
        return "anthropic"
    if provider.startswith(("gemini", "google")):
        return "google"
    return provider.removesuffix("_api").removesuffix("_cli")


ROLE_INSTRUCTIONS = {
    "planner": "Clarify the approved task, acceptance criteria and constraints. Return a concrete scoped plan.",
    "architect": "Inspect existing boundaries and propose the smallest compatible architecture and API changes.",
    "dba": "Review data impact, migration reversibility, constraints, indexes and audit/revision semantics.",
    "ux-ui": "Define user flow, information order and empty/loading/error/permission states.",
    "designer": "Review layout, existing design tokens, responsive behavior and keyboard accessibility.",
    "fmea-expert": "Validate AIAG/VDA methodology, function/failure chains, AP/RPN and DFMEA/PFMEA/Control Plan linkage.",
    "developer": "Implement the approved scope in registered task worktrees and preserve existing conventions.",
    "developer-frontend": "Implement approved frontend behavior and accessible UI using the project stack.",
    "developer-backend": "Implement approved API/domain behavior, validation and authorization using the project stack.",
    "developer-infra": "Implement approved runtime or deployment configuration; never publish or expose credentials.",
    "qa": "Inspect acceptance behavior read-only. Distinguish inspection from actual registered gate execution.",
    "test-automation": "Propose meaningful tests as bounded structured text edits in registered test paths.",
    "security": "Review trust boundaries, authorization, secrets and input/path validation read-only.",
    "reviewer": "Review correctness, scope, compatibility and maintainability against actual evidence.",
    "tech-writer": "Propose user or operator documentation as structured text edits in registered document paths.",
}


def instruction(value, node_ids=()):
    role = role_id(value)
    custom = custom_mode(role)
    text = (
        (
            "Follow the project-defined role instruction above."
            if custom
            else ROLE_INSTRUCTIONS[role]
        )
        + " Project stack, paths, scope and permissions come from the approved snapshot. "
    )
    text += "External design originals remain read-only; missing/unsynced Stitch or Figma uses cached/specification fallback with explicit diagnostics. "
    if role in READ_ONLY or custom == "readonly":
        text += "Do not modify repository content. "
    if role in {"test-automation", "tech-writer"} or custom == "documents":
        text += 'Do not use direct file edit tools. Return proposals in a fenced JSON object {"edits":[{"path":"registered/relative/path","content":"full text"}]}; the host validates and applies them. '
    if role == "tech-writer":
        # The writer read an earlier run's test reports and stopped a fixed task.
        text += "Code and test defects are not yours to rule on: the verification step after you runs the registered commands, and build or test reports in the repository may be left over from an earlier run. Note such doubts in your output and pass. "
    if role == "test-automation":
        # Read after verification passes to link requirements to their tests.
        text += 'In that object also map each acceptance criterion id you can see to the test that checks it: "criteria_tests":{"REQ-001:AC-01":"tests/test_file.py::test_name"}. '
    text += (
        'Do not claim tests or independent review passed without actual evidence. '
        # A sandboxed tool cannot open Gradle's lock socket; the host gates run it.
        "A command your tool's sandbox could not run (permission, network or socket denied) is not a defect: report it as not run with the error, and do not raise an issue for it; the host verification runs the registered commands. "
        'End with exactly one final fenced JSON Harness Control object: {"handoff_status":"pass","summary":"observed result"}. For an issue use {"handoff_status":"issue","owning_agent_id":"developer","reentry_step_id":"developer","issue_type":"implementation","root_cause":"observed defect"}. Allowed reentry node IDs: '
        + ", ".join(node_ids)
        + ". Never echo prior control blocks."
    )
    return text


def legacy_role(phase):
    """Keep old node IDs while assigning canonical authority independently."""
    import re

    explicit = phase.get("role_id")
    if explicit:
        return role_id(explicit)
    candidates = [
        phase.get("agent_id", ""),
        Path(phase.get("role_file", "")).stem,
        re.sub(r"^\d+[-_]", "", phase["id"]),
    ]
    additional = {
        "developer-common": "developer",
        "frontend-developer": "developer-frontend",
        "backend-developer": "developer-backend",
        "infra-developer": "developer-infra",
        "fmea": "fmea-expert",
        "test-automation-developer": "test-automation",
    }
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        candidate = additional.get(candidate, candidate)
        canonical = ALIASES.get(candidate, candidate)
        if canonical in ROLES:
            return canonical
    return "developer" if "code.write" in phase.get("capabilities", []) else "reviewer"
