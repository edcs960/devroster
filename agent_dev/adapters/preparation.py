"""Separate settings persistence from permission to start AI execution."""

from agent_dev.domain.errors import DomainError, ErrorCode

MESSAGE = "준비 모드에서는 AI 실행을 할 수 없습니다. 프로젝트 설정의 AI 도구에서 기본 AI를 활성화하고 인증을 확인해주세요."
AI_OPERATIONS = frozenset({"task.run", "task.resume"})


class PreparationModeError(DomainError):
    def __init__(self):
        super().__init__(ErrorCode.POLICY_VIOLATION, MESSAGE)


def is_preparation(root):
    from agent_dev.adapters.project_setup import ai_defaults

    ai = ai_defaults(root)
    return not ai["runners"][ai["provider"]]["enabled"]


def require_ai(root):
    if is_preparation(root):
        raise PreparationModeError()
