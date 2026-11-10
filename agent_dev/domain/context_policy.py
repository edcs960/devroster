"""Portable context policy; built-in Ponytail-inspired guidance never overrides roles."""
from agent_dev.domain.errors import DomainError, ErrorCode


def normalize(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - {"token_mode", "ponytail"}:
        raise DomainError(ErrorCode.INVALID_INPUT, "컨텍스트 정책 형식을 확인해주세요.")
    result = {"token_mode": value.get("token_mode", "balanced"), "ponytail": value.get("ponytail", "off")}
    if result["token_mode"] not in {"off", "balanced", "aggressive"} or result["ponytail"] not in {"off", "lite", "full", "ultra"}:
        raise DomainError(ErrorCode.INVALID_INPUT, "컨텍스트 절약 모드를 확인해주세요.")
    return result


def instructions(value):
    result = normalize(value)
    return dict(result, version="agent-dev-context-v1", external_skill_installed=False,
        guidance=("Prefer existing code, standard library and platform features. Avoid unnecessary dependencies. "
                  "Preserve security, accessibility, error handling, required verification and DB/FMEA constraints. "
                  "Do not delete code solely to reduce tokens.") if result["ponytail"] != "off" else "",
        detail={"off": "original", "lite": "conservative reuse", "full": "review dependency alternatives", "ultra": "explain smallest maintainable option"}[result["ponytail"]])


# Readable names for the prompt sections shown in the execution view (#21 in the follow-up design).
SECTION_LABELS = {
    "approved_design": "승인된 설계",
    "wiki_verified_data": "위키",
    "knowledge_verified_data": "검증된 지식",
    "project_rules": "프로젝트 규칙",
    "project_skills": "스킬",
    "previous_evidence_untrusted": "이전 단계 결과",
    "findings_untrusted": "지적 사항",
    "repositories": "저장소 정보",
    "task": "작업 정의",
    "design_reference_bundles_untrusted": "화면 참고 자료",
    "tool_evidence_untrusted": "도구 결과",
    "operating_rules": "운영 규칙",
    "role_instruction": "역할 지시",
    "nodes": "세션 노드별 입력",
}


def profile(context, top=8):
    """Size of each top-level section of a role prompt (characters), largest first.

    Measuring comes before trimming: the execution view shows where a prompt's length comes
    from, so a budget can later cut what matters least. Nothing here changes the prompt.
    """
    import json

    try:
        value = json.loads(context)
    except (TypeError, ValueError):
        return {"total": len(context or ""), "sections": []}
    if not isinstance(value, dict):
        return {"total": len(context), "sections": []}
    sizes = sorted(
        ((key, len(json.dumps(item, ensure_ascii=False))) for key, item in value.items()),
        key=lambda pair: -pair[1],
    )
    return {
        "total": len(context),
        "sections": [
            {"key": key, "label": SECTION_LABELS.get(key, key), "chars": chars} for key, chars in sizes[:top]
        ],
    }
