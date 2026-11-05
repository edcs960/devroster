"""Public provider metadata and validated, credential-free runner configuration."""

import copy
import ipaddress
from urllib.parse import urlsplit

from agent_dev.adapters.environment import validate_key
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.model_routing import EFFORTS, model_text, routing_policy

PROVIDERS = {
    "codex_cli": {"label": "Codex CLI", "kind": "cli", "command": "codex", "login": "codex login"},
    "claude_cli": {
        "label": "Claude CLI",
        "kind": "cli",
        "command": "claude",
        "login": "claude auth login",
    },
    "gemini_cli": {
        "label": "Gemini CLI",
        "kind": "cli",
        "command": "gemini",
        "login": "gemini (로그인 선택)",
    },
    "openai_api": {
        "label": "OpenAI API",
        "kind": "api",
        "base_url": "https://api.openai.com/v1",
        "credential_ref": "OPENAI_API_KEY",
    },
    "anthropic_api": {
        "label": "Claude API",
        "kind": "api",
        "base_url": "https://api.anthropic.com/v1",
        "credential_ref": "ANTHROPIC_API_KEY",
    },
    "gemini_api": {
        "label": "Gemini API",
        "kind": "api",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "credential_ref": "GEMINI_API_KEY",
    },
    "xai_api": {
        "label": "Grok API (xAI)",
        "kind": "api",
        "base_url": "https://api.x.ai/v1",
        "credential_ref": "XAI_API_KEY",
    },
    "compatible_api": {
        "label": "OpenAI 호환·로컬 API",
        "kind": "api",
        "base_url": "http://127.0.0.1:11434/v1",
        "credential_ref": "",
    },
}
CLI_PROVIDERS = tuple(p for p, row in PROVIDERS.items() if row["kind"] == "cli")
API_PROVIDERS = tuple(p for p, row in PROVIDERS.items() if row["kind"] == "api")


def invalid(message):
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def metadata():
    return copy.deepcopy(PROVIDERS)


def defaults(provider):
    row = PROVIDERS[provider]
    config = {"enabled": False, "model": "", "effort": "", "routing": routing_policy()}
    if row["kind"] == "cli":
        config["command"] = row["command"]
    else:
        config.update(
            base_url=row["base_url"],
            credential_ref=row["credential_ref"],
            max_output_tokens=16384,
            reasoning="none" if provider == "compatible_api" else "native",
        )
    return config


def endpoint(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1000
        or any(c.isspace() for c in value)
    ):
        invalid("API 주소를 확인해주세요.")
    try:
        url = urlsplit(value)
        port = url.port
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or (port is not None and port == 0)
        ):
            invalid("API 주소에는 인증 값·쿼리·fragment를 넣을 수 없습니다.")
        host = (url.hostname or "").lower()
        local = host == "localhost"
        try:
            address = ipaddress.ip_address(host)
            local = address.is_loopback
            if not local and not address.is_global:
                invalid(
                    "API 주소에 내부·메타데이터 IP를 사용할 수 없습니다. 로컬은 loopback 주소를 사용해주세요."
                )
        except ValueError:
            if (
                host.endswith((".localhost", ".local", ".internal"))
                or "." not in host
                and not local
            ):
                invalid("API 주소를 공개 HTTPS 또는 loopback 주소로 지정해주세요.")
        if url.scheme != "https" and not (url.scheme == "http" and local):
            invalid("API는 HTTPS, 로컬 서버는 loopback HTTP 주소를 사용해주세요.")
        if (
            any(part in {".", ".."} for part in url.path.split("/"))
            or "%" in url.path
            or "\\" in value
        ):
            invalid("API 경로를 확인해주세요.")
    except ValueError:
        invalid("API 주소·포트 형식을 확인해주세요.")
    return value.rstrip("/")


def normalize(provider, value):
    if provider not in PROVIDERS or not isinstance(value, dict):
        invalid("지원하는 AI 도구를 선택해주세요.")
    result = defaults(provider)
    if set(value) - set(result):
        invalid("AI 도구 설정 형식을 확인해주세요.")
    result.update(copy.deepcopy(value))
    if type(result["enabled"]) is not bool:
        invalid("AI 도구 사용 여부를 선택해주세요.")
    result["model"] = model_text(result["model"])
    result["routing"] = routing_policy(result["routing"])
    if result["effort"] not in ("", *EFFORTS):
        invalid("추론 강도를 확인해주세요.")
    if provider in CLI_PROVIDERS:
        command = result["command"]
        if (
            not isinstance(command, str)
            or not command.strip()
            or len(command) > 1000
            or any(ord(c) < 32 for c in command)
        ):
            invalid("CLI 실행파일을 확인해주세요.")
        result["command"] = command.strip()
    else:
        result["base_url"] = endpoint(result["base_url"])
        if result["credential_ref"]:
            validate_key(result["credential_ref"])
        elif provider != "compatible_api":
            invalid("API 인증 환경변수 이름이 필요합니다.")
        if (
            type(result["max_output_tokens"]) is not int
            or not 256 <= result["max_output_tokens"] <= 65536
        ):
            invalid("API 출력 토큰 상한은256~65536으로 지정해주세요.")
        if result["reasoning"] not in {"none", "native"}:
            invalid("API 추론 옵션을 확인해주세요.")
        if result["enabled"] and not result["model"] and result["routing"]["mode"] != "auto":
            invalid("API에는 사용할 모델 이름을 지정해주세요.")
    efforts = [result["effort"]]
    if result["routing"]["mode"] == "auto":
        efforts += [
            level
            for row in result["routing"]["profiles"].values()
            for level in row["supported_efforts"]
        ]
    if provider == "gemini_cli" and any(efforts):
        invalid(
            "Gemini CLI의 강도는 CLI 기본값을 사용합니다. 고정 강도·지원 강도 목록을 비워주세요. 강도 지정은 Gemini API에서 지원합니다."
        )
    if provider in API_PROVIDERS and result["reasoning"] == "none" and any(efforts):
        invalid("추론 옵션을 사용하려면 native를 선택하거나 강도 설정을 비워주세요.")
    if provider == "gemini_api" and any(e in {"xhigh", "max"} for e in efforts):
        invalid("Gemini API의 thinkingLevel은 low/medium/high 중 모델 지원 범위로 지정해주세요.")
    if provider == "xai_api" and any(e in {"xhigh", "max"} for e in efforts):
        invalid(
            "Grok API의 reasoning_effort는 low/medium/high 중 모델 지원 범위로 지정해주세요. 지원하지 않는 모델은 강도를 비워주세요."
        )
    if provider == "anthropic_api" and "xhigh" in efforts:
        invalid("Claude API의 effort는 low/medium/high/max 중 모델 지원 범위로 지정해주세요.")
    return result


def credential_ready(provider, config, environment):
    key = config.get("credential_ref", "")
    return provider in API_PROVIDERS and (not key or bool(environment.get(key)))


def gemini_auth_ready(environment):
    """Local readiness only, without reading OAuth token values or making an AI call."""
    import json
    import os
    from pathlib import Path

    if environment.get("GEMINI_API_KEY"):
        return True
    folder = Path(os.environ.get("GEMINI_CLI_HOME", str(Path.home()))) / ".gemini"
    settings, credentials = folder / "settings.json", folder / "oauth_creds.json"
    if settings.is_symlink() or not settings.is_file() or settings.stat().st_size > 100_000:
        return False
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        return (
            data.get("security", {}).get("auth", {}).get("selectedType") == "oauth-personal"
            and credentials.is_file()
            and not credentials.is_symlink()
        )
    except (OSError, ValueError, AttributeError):
        return False
