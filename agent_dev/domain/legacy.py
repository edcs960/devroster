"""Explicit, read-only legacy conversion. Never rewrite a legacy status on read."""

from types import MappingProxyType

from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.status import TaskStatus

LEGACY_TASK_STATUSES = MappingProxyType(
    {
        "in_progress": TaskStatus.RUNNING,
        # Phase completion is not verification or PR completion.
        "review_ready": TaskStatus.VERIFYING,
        "verification_failed": TaskStatus.BLOCKED,
        "feedback": TaskStatus.FEEDBACK_REQUIRED,
        "done": TaskStatus.COMPLETED,
        "waiting_capacity": TaskStatus.WAITING_FOR_CAPACITY,
    }
)


def parse_task_status(value: str) -> TaskStatus:
    """Accept canonical names and documented aliases; reject unknowns, unchanged.

    Several aliases are lossy (blocked vs verification_failed). The caller must
    retain the original legacy record; this projection cannot round-trip it.
    """
    if not isinstance(value, str):
        raise DomainError(ErrorCode.INVALID_INPUT, "Task status must be a string.")
    if value in LEGACY_TASK_STATUSES:
        return LEGACY_TASK_STATUSES[value]
    try:
        return TaskStatus(value)
    except ValueError:
        raise DomainError(ErrorCode.INVALID_INPUT, "Unknown task status.") from None
