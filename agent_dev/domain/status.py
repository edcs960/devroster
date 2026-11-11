"""Canonical lifecycle states, separate from legacy storage strings."""

from enum import StrEnum


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    ACTIVE = "active"
    REVIEW_PENDING = "review_pending"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    PLANNED = "planned"
    APPROVAL_PENDING = "approval_pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    WAITING_FOR_CAPACITY = "waiting_for_capacity"
    VERIFYING = "verifying"
    PR_READY = "pr_ready"
    USER_REVIEW = "user_review"
    FEEDBACK_REQUIRED = "feedback_required"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PhaseAttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SUPERSEDED = "superseded"
    WAITING_FOR_CAPACITY = "waiting_for_capacity"


# Plain-language (label, explanation) per task state, for screens and messages.
STATE_TEXT = {
    "planned": ("계획 작성 중", "요구사항과 실행 계획을 정리하는 단계입니다."),
    "approval_pending": ("승인을 기다려요", "설계와 계획을 확인한 뒤 승인하면 작업을 준비합니다."),
    "ready": ("시작할 수 있어요", "작업 준비가 끝났습니다. 필요한 조치를 확인하세요."),
    "running": ("작업 진행 중", "AI가 작업 중입니다. 진행 단계와 결과가 자동으로 갱신됩니다."),
    "blocked": ("문제 해결이 필요해요", "작업이 멈췄습니다. 아래 원인과 해결 안내를 확인하세요."),
    "waiting_for_capacity": (
        "실행 여유를 기다려요",
        "사용량 한도 또는 실행 가능한 AI가 준비될 때까지 기다립니다.",
    ),
    "verifying": (
        "결과 검증이 필요해요",
        "개발 단계가 끝났습니다. 테스트와 필수 검사를 확인하세요.",
    ),
    "pr_ready": ("변경사항 검토 준비", "검증이 끝났습니다. 변경사항과 게시 정책을 검토하세요."),
    "user_review": ("최종 확인을 기다려요", "PR과 CI 결과를 확인하고 완료 여부를 결정하세요."),
    "feedback_required": ("수정 요청을 반영해요", "피드백을 확인하고 수정 계획을 승인해야 합니다."),
    "completed": ("완료", "작업이 완료되었습니다. 기록과 검증 결과를 확인할 수 있습니다."),
    "cancelled": ("취소됨", "취소된 작업입니다. 기존 기록은 계속 확인할 수 있습니다."),
}
