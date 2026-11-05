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
