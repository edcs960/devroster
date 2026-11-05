"""Pure transition policies; facts are supplied by the application, never inferred.

These policies do not authorize legacy execution or perform persistence.
Callers must gather trustworthy evidence and commit with an optimistic lock.
"""

from dataclasses import dataclass
from types import MappingProxyType

from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.status import PhaseAttemptStatus, ProjectStatus, TaskStatus


@dataclass(frozen=True)
class TransitionContext:
    design_approved: bool = False
    plan_approved: bool = False
    snapshot_created: bool = False
    dependencies_completed: bool = False
    preflight_passed: bool = False
    lease_acquired: bool = False
    capacity_exhausted: bool = False
    phases_passed: bool = False
    verification_passed: bool = False
    pr_created: bool = False
    ci_passed: bool = False
    user_approved: bool = False
    merge_policy_satisfied: bool = False
    no_blocking_findings: bool = False
    expected_version: int | None = None
    current_version: int | None = None


PROJECT_TRANSITIONS = MappingProxyType(
    {
        ProjectStatus.DRAFT: frozenset({ProjectStatus.PLANNING, ProjectStatus.ARCHIVED}),
        ProjectStatus.PLANNING: frozenset({ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED}),
        ProjectStatus.ACTIVE: frozenset({ProjectStatus.REVIEW_PENDING, ProjectStatus.ARCHIVED}),
        ProjectStatus.REVIEW_PENDING: frozenset({ProjectStatus.COMPLETED, ProjectStatus.ARCHIVED}),
        ProjectStatus.COMPLETED: frozenset({ProjectStatus.ARCHIVED}),
        ProjectStatus.ARCHIVED: frozenset(),
    }
)

_TASK_FORWARD = {
    TaskStatus.PLANNED: {TaskStatus.APPROVAL_PENDING},
    TaskStatus.APPROVAL_PENDING: {TaskStatus.READY},
    TaskStatus.READY: {TaskStatus.RUNNING},
    TaskStatus.RUNNING: {TaskStatus.VERIFYING, TaskStatus.WAITING_FOR_CAPACITY},
    TaskStatus.WAITING_FOR_CAPACITY: {TaskStatus.READY},
    TaskStatus.VERIFYING: {TaskStatus.PR_READY},
    TaskStatus.PR_READY: {TaskStatus.USER_REVIEW},
    TaskStatus.USER_REVIEW: {TaskStatus.COMPLETED, TaskStatus.FEEDBACK_REQUIRED},
    TaskStatus.FEEDBACK_REQUIRED: {TaskStatus.APPROVAL_PENDING},
    TaskStatus.BLOCKED: {TaskStatus.READY},
    TaskStatus.COMPLETED: set(),
    TaskStatus.CANCELLED: set(),
}
TASK_TRANSITIONS = MappingProxyType(
    {
        state: frozenset(
            targets | ({TaskStatus.BLOCKED, TaskStatus.CANCELLED} - {state})
            if state not in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
            else targets
        )
        for state, targets in _TASK_FORWARD.items()
    }
)
del _TASK_FORWARD

PHASE_ATTEMPT_TRANSITIONS = MappingProxyType(
    {
        PhaseAttemptStatus.QUEUED: frozenset(
            {
                PhaseAttemptStatus.RUNNING,
                PhaseAttemptStatus.WAITING_FOR_CAPACITY,
            }
        ),
        PhaseAttemptStatus.RUNNING: frozenset(
            {
                PhaseAttemptStatus.PASSED,
                PhaseAttemptStatus.FAILED,
                PhaseAttemptStatus.INTERRUPTED,
                PhaseAttemptStatus.SUPERSEDED,
                PhaseAttemptStatus.WAITING_FOR_CAPACITY,
            }
        ),
        PhaseAttemptStatus.PASSED: frozenset(),
        PhaseAttemptStatus.FAILED: frozenset(),
        PhaseAttemptStatus.INTERRUPTED: frozenset(),
        PhaseAttemptStatus.SUPERSEDED: frozenset(),
        # Resuming capacity creates a new attempt, not a queued version of this one.
        PhaseAttemptStatus.WAITING_FOR_CAPACITY: frozenset(),
    }
)


def _check_version(context: TransitionContext) -> None:
    expected, current = context.expected_version, context.current_version
    if expected is None and current is None:
        return
    if type(expected) is not int or type(current) is not int or expected < 0 or current < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "Both versions must be non-negative integers.")
    if expected != current:
        raise DomainError(ErrorCode.STALE_VERSION, "Aggregate version changed; reload state.")


def _require(fact: bool, code: ErrorCode, message: str) -> None:
    if fact is not True:
        raise DomainError(code, message)


_DEFAULT_CONTEXT = TransitionContext()


def transition_task(
    current: TaskStatus,
    target: TaskStatus,
    context: TransitionContext = _DEFAULT_CONTEXT,
) -> TaskStatus:
    if not isinstance(current, TaskStatus) or not isinstance(target, TaskStatus):
        raise DomainError(ErrorCode.INVALID_INPUT, "Task transitions require TaskStatus values.")
    _check_version(context)
    if target not in TASK_TRANSITIONS[current]:
        raise DomainError(ErrorCode.POLICY_VIOLATION, "Task transition is not allowed.")

    if target == TaskStatus.READY:
        _require(
            context.design_approved,
            ErrorCode.APPROVAL_REQUIRED,
            "Current design approval required.",
        )
        _require(
            context.plan_approved, ErrorCode.APPROVAL_REQUIRED, "Current plan approval required."
        )
        _require(context.snapshot_created, ErrorCode.POLICY_VIOLATION, "Input snapshot required.")
    elif target == TaskStatus.RUNNING:
        _require(
            context.dependencies_completed, ErrorCode.POLICY_VIOLATION, "Dependencies incomplete."
        )
        _require(context.preflight_passed, ErrorCode.POLICY_VIOLATION, "Preflight required.")
        _require(context.lease_acquired, ErrorCode.LEASE_CONFLICT, "Task lease required.")
    elif target == TaskStatus.VERIFYING:
        _require(context.phases_passed, ErrorCode.POLICY_VIOLATION, "Applicable phases incomplete.")
    elif target == TaskStatus.WAITING_FOR_CAPACITY:
        _require(
            context.capacity_exhausted,
            ErrorCode.POLICY_VIOLATION,
            "Eligible runners not exhausted.",
        )
    elif target == TaskStatus.PR_READY:
        _require(
            context.verification_passed, ErrorCode.VERIFICATION_FAILED, "Required gates failed."
        )
    elif target == TaskStatus.USER_REVIEW:
        _require(context.pr_created, ErrorCode.POLICY_VIOLATION, "Pull request required.")
        _require(context.ci_passed, ErrorCode.VERIFICATION_FAILED, "Required CI checks failed.")
    elif target == TaskStatus.COMPLETED:
        _require(context.user_approved, ErrorCode.APPROVAL_REQUIRED, "User approval required.")
        _require(
            context.merge_policy_satisfied, ErrorCode.POLICY_VIOLATION, "Merge policy unsatisfied."
        )
        _require(
            context.no_blocking_findings, ErrorCode.POLICY_VIOLATION, "Blocking findings remain."
        )
    return target


def transition_project(current: ProjectStatus, target: ProjectStatus) -> ProjectStatus:
    if not isinstance(current, ProjectStatus) or not isinstance(target, ProjectStatus):
        raise DomainError(
            ErrorCode.INVALID_INPUT, "Project transitions require ProjectStatus values."
        )
    if target not in PROJECT_TRANSITIONS[current]:
        raise DomainError(ErrorCode.POLICY_VIOLATION, "Project transition is not allowed.")
    return target


def transition_phase_attempt(
    current: PhaseAttemptStatus,
    target: PhaseAttemptStatus,
) -> PhaseAttemptStatus:
    if not isinstance(current, PhaseAttemptStatus) or not isinstance(target, PhaseAttemptStatus):
        raise DomainError(
            ErrorCode.INVALID_INPUT, "Phase transitions require PhaseAttemptStatus values."
        )
    if target not in PHASE_ATTEMPT_TRANSITIONS[current]:
        raise DomainError(ErrorCode.POLICY_VIOLATION, "Phase attempt transition is not allowed.")
    return target
