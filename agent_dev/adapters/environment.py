"""Project environment values are private files, never snapshot or backup content."""

import json
import os
import re
from pathlib import Path

from agent_dev.adapters.workspace import configuration_root
from agent_dev.domain.errors import DomainError, ErrorCode

MASK = "••••••••"
DENIED = {
    "HOME",
    "USERPROFILE",
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "NODE_OPTIONS",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "GEMINI_CLI_HOME",
    "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
    "GEMINI_CLI_SYSTEM_DEFAULTS_PATH",
    "GEMINI_CLI_TRUSTED_FOLDERS_PATH",
    "GEMINI_SYSTEM_MD",
    "BASH_ENV",
    "ENV",
    "SHELLOPTS",
    "GIT_CONFIG",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_SSH_COMMAND",
}


def environment_file(root):
    from agent_dev.adapters.workspace_locator import workspace_locations

    return workspace_locations(configuration_root(Path(root))).environment_file


def validate_key(key):
    if (
        not isinstance(key, str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", key)
        or key in DENIED
        or key.startswith(("LD_", "DYLD_", "GIT_CONFIG_", "PYTHON"))
    ):
        raise DomainError(
            ErrorCode.INVALID_INPUT,
            "환경변수 이름을 확인해주세요. 실행 환경을 바꾸는 시스템 키는 사용할 수 없습니다.",
        )


def read_environment(root):
    path = environment_file(root)
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 500_000:
        raise DomainError(ErrorCode.POLICY_VIOLATION, "환경변수 저장 파일을 확인해주세요.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or len(value) > 100:
        raise DomainError(ErrorCode.INVALID_INPUT, "환경변수 파일 형식이 올바르지 않습니다.")
    for key, item in value.items():
        validate_key(key)
        if not isinstance(item, str) or len(item) > 8000 or "\x00" in item:
            raise DomainError(ErrorCode.INVALID_INPUT, "환경변수 값 형식을 확인해주세요.")
    return value


def process_environment(root, keys):
    if not isinstance(keys, list) or len(keys) > 100:
        raise DomainError(ErrorCode.INVALID_INPUT, "승인된 환경변수 이름 목록이 필요합니다.")
    for key in keys:
        validate_key(key)
    values = read_environment(root)
    missing = [key for key in keys if key not in values]
    if missing:
        raise DomainError(
            ErrorCode.INVALID_INPUT, "환경변수 값을 다시 설정해주세요: " + ", ".join(missing)
        )
    from agent_dev.core.cli_home import isolated_environment

    return isolated_environment(dict(os.environ, **{key: values[key] for key in keys}))
