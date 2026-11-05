"""Field-by-field composition of shared defaults and project overrides.

Only registered fields compose. A missing project key inherits; an explicit empty
string is a value ("use the CLI default"); ``null`` is rejected so it is never
confused with inheritance. Lists replace rather than merge, and there is no
recursive dictionary merge. Execution scope (repositories, allow-paths,
verification, gates, execute/publish flags) and credentials are never inherited.
"""

import copy
from typing import NoReturn

from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.execution_limits import BOUNDS, DEFAULT_LIMITS

TEXT_FIELDS = ("provider", "model", "effort")
GROUPS = {
    "ai": {key: "text" for key in TEXT_FIELDS},
    "limits": {key: "limit" for key in BOUNDS},
}
# Shared-only groups: caps bound every project override; templates are copied on request.
SHARED_GROUPS = {
    "limit_caps": {key: "limit" for key in BOUNDS},
    "templates": {"roles": "roster"},
}
DEFAULTS = {"ai": dict.fromkeys(TEXT_FIELDS, ""), "limits": dict(DEFAULT_LIMITS)}
NEVER_INHERITED = (
    "repositories",
    "allowed_paths",
    "test_paths",
    "document_paths",
    "verification",
    "orchestrator_v2",
    "environment",
    "environment_keys",
    "credential_ref",
)


def invalid(message) -> NoReturn:
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def _text(path, value):
    if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
        invalid(f"{path} 값은 200자 이하의 한 줄 문자열이어야 합니다.")
    return value.strip()


def _limit(path, value):
    low, high = BOUNDS[path.split(".", 1)[1]]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        invalid(f"{path} 값은 {low}~{high} 사이의 정수여야 합니다.")
    return value


def _validate(value, groups, label):
    if not isinstance(value, dict):
        invalid(f"{label}은(는) 객체여야 합니다.")
    blocked = sorted(set(value) & set(NEVER_INHERITED))
    if blocked:
        invalid(f"{', '.join(blocked)}은(는) 공통 설정으로 상속하거나 지정할 수 없습니다.")
    if set(value) - set(groups):
        invalid(
            f"{label}에 지원하지 않는 항목이 있습니다: {', '.join(sorted(set(value) - set(groups)))}"
        )
    result = {}
    for group, fields in groups.items():
        if group not in value:
            continue
        items = value[group]
        if not isinstance(items, dict):
            invalid(f"{group} 값은 객체여야 합니다. 상속하려면 항목을 지우세요.")
        if set(items) - set(fields):
            invalid(f"{group}에 지원하지 않는 항목이 있습니다.")
        checked = {}
        for key, item in items.items():
            path = f"{group}.{key}"
            if item is None:
                invalid(f"{path}: null 대신 항목을 지우면 공통값을 상속합니다.")
            kind = fields[key]
            if kind == "text":
                checked[key] = _text(path, item)
            elif kind == "limit":
                checked[key] = _limit(path, item)
            else:
                from agent_dev.domain.project_roles import normalize

                checked[key] = normalize(item)
        result[group] = checked
    return result


def validate_overrides(overrides) -> dict:
    """Normalized project overrides; only ``ai`` and ``limits`` fields."""
    return _validate(overrides, GROUPS, "프로젝트 지정값")


def validate_global(values) -> dict:
    """Normalized shared defaults, with every default within its own cap."""
    result = _validate(values, {**GROUPS, **SHARED_GROUPS}, "공통 설정")
    _check_caps(result.get("limits", {}), result.get("limit_caps", {}), "공통 기본값")
    return result


def _check_caps(limits, caps, label):
    for key, value in limits.items():
        if key in caps and value > caps[key]:
            invalid(f"{label} limits.{key}={value}이(가) 공통 상한 {caps[key]}을(를) 넘습니다.")


def resolve_settings(global_values: dict, project_overrides: dict) -> dict:
    """``{"values": ..., "origins": {"group.key": "default"|"global"|"project"}}``."""
    shared = validate_global(global_values)
    overrides = validate_overrides(project_overrides)
    _check_caps(overrides.get("limits", {}), shared.get("limit_caps", {}), "프로젝트 지정값")
    values = copy.deepcopy(DEFAULTS)
    origins = {}
    for group, fields in GROUPS.items():
        for key in fields:
            path = f"{group}.{key}"
            origins[path] = "default"
            for origin, source in (("global", shared), ("project", overrides)):
                if key in source.get(group, {}):
                    values[group][key] = source[group][key]
                    origins[path] = origin
    # Caps bound every effective value: a built-in default above a shared cap is the cap.
    for key, cap in bounded_caps(shared).items():
        if values["limits"][key] > cap:
            values["limits"][key] = cap
            origins["limits." + key] = "global"
    return {"values": values, "origins": origins}


def bounded_caps(shared) -> dict:
    return dict((shared or {}).get("limit_caps", {}))


def check_limits(limits, global_values):
    """Refuse a project's saved execution limits above the shared caps (§4.1)."""
    _check_caps(limits or {}, bounded_caps(global_values), "프로젝트 실행 한도")
