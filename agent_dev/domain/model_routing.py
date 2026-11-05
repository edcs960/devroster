"""Reproducible model selection inside the registered, approved runner policy."""

import copy
import re

from agent_dev.domain.errors import DomainError, ErrorCode

EFFORTS = ("low", "medium", "high", "xhigh", "max")
TIERS = ("simple", "standard", "complex")
TARGETS = dict(zip(TIERS, EFFORTS[:3]))


def invalid(message):
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def model_text(value):
    if not isinstance(value, str) or len(value) > 100 or any(ord(c) < 32 for c in value):
        invalid("모델 이름 형식을 확인해주세요.")
    value = value.strip()
    if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*", value):
        invalid("모델 이름에 공백·명령 옵션을 사용할 수 없습니다.")
    return value


def routing_policy(value=None):
    policy = copy.deepcopy(value) if value is not None else {}
    if not isinstance(policy, dict) or set(policy) - {"mode", "max_effort", "profiles"}:
        invalid("모델 자동 선택 정책 형식을 확인해주세요.")
    policy.setdefault("mode", "fixed")
    policy.setdefault("max_effort", "high")
    policy.setdefault("profiles", {})
    if policy["mode"] not in ("fixed", "auto") or policy["max_effort"] not in EFFORTS[:3]:
        invalid("고정/자동 선택과 추론 강도 상한(low/medium/high)을 확인해주세요.")
    if not isinstance(policy["profiles"], dict) or set(policy["profiles"]) - set(TIERS):
        invalid("단순/일반/복잡 작업의 모델 설정을 확인해주세요.")
    for tier in TIERS:
        row = policy["profiles"].setdefault(tier, {})
        if not isinstance(row, dict) or set(row) - {"model", "supported_efforts"}:
            invalid("작업별 모델 설정 형식을 확인해주세요.")
        row["model"] = model_text(row.get("model", ""))
        levels = row.get("supported_efforts", [])
        if (
            not isinstance(levels, list)
            or len(levels) > len(EFFORTS)
            or any(level not in EFFORTS for level in levels)
            or len(set(levels)) != len(levels)
        ):
            invalid("모델이 지원하는 추론 강도 목록을 확인해주세요.")
        row["supported_efforts"] = levels
        if policy["mode"] == "auto":
            if not row["model"]:
                invalid("자동 선택에는 단순/일반/복잡 작업의 모델을 모두 등록해주세요.")
            effective_effort(tier, policy)
    return policy


def effective_effort(tier, policy):
    levels = policy["profiles"][tier]["supported_efforts"]
    if not levels:
        return ""  # Explicitly registered as using the provider's default.
    ceiling = min(EFFORTS.index(TARGETS[tier]), EFFORTS.index(policy["max_effort"]))
    eligible = [level for level in levels if EFFORTS.index(level) <= ceiling]
    if not eligible:
        invalid("등록한 모델의 지원 강도가 작업 목표·설정 상한보다 높습니다.")
    return max(eligible, key=EFFORTS.index)


def classify(task, phase_id):
    phase = phase_id.lower()
    text = " ".join(
        str(task.get(key, "")) for key in ("title", "scope", "workstreams", "acceptance_criteria")
    ).lower()
    if any(
        role in phase
        for role in ("planner", "architect", "dba", "security", "reviewer", "fmea", "studio-specs")
    ):
        return "complex", "설계·명세 종합 또는 위험 검토 단계"
    risky = (
        "보안",
        "인증",
        "권한",
        "마이그레이션",
        "동시성",
        "결제",
        "스키마",
        "security",
        "authentication",
        "authorization",
        "migration",
        "concurrency",
        "schema",
        "payment",
    )
    if any(word in text for word in risky) or re.search(r"\b(db|database)\b", text):
        return "complex", "인증·보안·데이터 등 위험 신호가 있는 작업"
    if (
        len(task.get("workstreams", [])) >= 3
        or len(task.get("scope", [])) >= 6
        or len(text) > 12_000
    ):
        return "complex", "여러 영역에 걸치거나 범위가 큰 작업"
    title = str(task.get("title", "")).lower()
    small_change = any(
        word in title
        for word in (
            "문서",
            "문구",
            "오탈자",
            "스타일",
            "readme",
            "documentation",
            "typo",
            "rename",
            "css",
        )
    )
    implementation = any(
        word in title for word in ("구현", "기능", "개발", "api", "implement", "feature")
    )
    if "tech-writer" in phase or (
        small_change and not implementation and len(task.get("scope", [])) <= 3
    ):
        return "simple", "문서·표현·이름 변경 중심의 작업"
    return "standard", "일반 구현·테스트 또는 분류가 불확실한 작업"


def select_model(config, task, phase_id):
    policy = routing_policy(config.get("routing"))
    fixed_effort = config.get("effort", "")
    if fixed_effort not in ("", *EFFORTS):
        invalid("추론 강도 설정을 확인해주세요.")
    tier, reason = classify(task, phase_id)
    model, effort = model_text(config.get("model", "")), fixed_effort
    if policy["mode"] == "auto":
        model = policy["profiles"][tier]["model"]
        effort = effective_effort(tier, policy)
        if not effort:
            reason += "; 지원 강도 미등록: CLI 기본 강도 사용"
        elif effort != TARGETS[tier]:
            reason += "; 설정 상한·모델 지원 범위로 강도 제한"
    else:
        reason = "사용자가 지정한 고정 모델·추론 강도"
    return {
        "policy_version": 1,
        "mode": policy["mode"],
        "tier": tier,
        "model": model,
        "effort": effort,
        "requested_effort": TARGETS[tier],
        "reason": reason,
    }
