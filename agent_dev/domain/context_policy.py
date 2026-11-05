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
