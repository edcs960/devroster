"""P0-02 acceptance contracts, independent of persistence and provider adapters."""

from dataclasses import FrozenInstanceError, replace
from itertools import product

import pytest

from agent_dev.core.models import Task
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.legacy import parse_task_status
from agent_dev.domain.status import PhaseAttemptStatus, ProjectStatus, TaskStatus
from agent_dev.domain.transitions import (
    TransitionContext,
    transition_phase_attempt,
    transition_project,
    transition_task,
)

# Expected edges come from the approved state model design, not
# the implementation's transition dictionaries.
TASK_EDGES = {
    ("planned", "approval_pending"),
    ("approval_pending", "ready"),
    ("ready", "running"),
    ("running", "verifying"),
    ("running", "waiting_for_capacity"),
    ("waiting_for_capacity", "ready"),
    ("verifying", "pr_ready"),
    ("pr_ready", "user_review"),
    ("user_review", "completed"),
    ("user_review", "feedback_required"),
    ("feedback_required", "approval_pending"),
    ("blocked", "ready"),
}
for source in ("planned", "approval_pending", "ready", "running", "waiting_for_capacity",
               "verifying", "pr_ready", "user_review", "feedback_required", "blocked"):
    TASK_EDGES.add((source, "cancelled"))
    if source != "blocked":
        TASK_EDGES.add((source, "blocked"))

PROJECT_EDGES = {
    ("draft", "planning"), ("planning", "active"), ("active", "review_pending"),
    ("review_pending", "completed"),
    ("draft", "archived"), ("planning", "archived"), ("active", "archived"),
    ("review_pending", "archived"), ("completed", "archived"),
}
PHASE_EDGES = {
    ("queued", "running"), ("queued", "waiting_for_capacity"),
    ("running", "passed"), ("running", "failed"), ("running", "interrupted"),
    ("running", "superseded"), ("running", "waiting_for_capacity"),
}

EVIDENCE = TransitionContext(
    design_approved=True, plan_approved=True, snapshot_created=True,
    dependencies_completed=True, preflight_passed=True, lease_acquired=True,
    capacity_exhausted=True, phases_passed=True, verification_passed=True, pr_created=True,
    ci_passed=True, user_approved=True, merge_policy_satisfied=True, no_blocking_findings=True,
    expected_version=4, current_version=4,
)


@pytest.mark.parametrize("current,target", list(product(TaskStatus, repeat=2)))
def test_every_task_state_pair_obeys_contract(current, target):
    if (current.value, target.value) in TASK_EDGES:
        assert transition_task(current, target, EVIDENCE) is target
    else:
        with pytest.raises(DomainError) as exc:
            transition_task(current, target, EVIDENCE)
        assert exc.value.code is ErrorCode.POLICY_VIOLATION


@pytest.mark.parametrize("current,target", list(product(ProjectStatus, repeat=2)))
def test_every_project_state_pair_obeys_contract(current, target):
    if (current.value, target.value) in PROJECT_EDGES:
        assert transition_project(current, target) is target
    else:
        with pytest.raises(DomainError) as exc:
            transition_project(current, target)
        assert exc.value.code is ErrorCode.POLICY_VIOLATION


@pytest.mark.parametrize("current,target", list(product(PhaseAttemptStatus, repeat=2)))
def test_every_phase_state_pair_obeys_contract(current, target):
    if (current.value, target.value) in PHASE_EDGES:
        assert transition_phase_attempt(current, target) is target
    else:
        with pytest.raises(DomainError) as exc:
            transition_phase_attempt(current, target)
        assert exc.value.code is ErrorCode.POLICY_VIOLATION


@pytest.mark.parametrize("source,target,field,code", [
    ("approval_pending", "ready", "design_approved", ErrorCode.APPROVAL_REQUIRED),
    ("approval_pending", "ready", "plan_approved", ErrorCode.APPROVAL_REQUIRED),
    ("approval_pending", "ready", "snapshot_created", ErrorCode.POLICY_VIOLATION),
    ("ready", "running", "dependencies_completed", ErrorCode.POLICY_VIOLATION),
    ("ready", "running", "preflight_passed", ErrorCode.POLICY_VIOLATION),
    ("ready", "running", "lease_acquired", ErrorCode.LEASE_CONFLICT),
    ("running", "waiting_for_capacity", "capacity_exhausted", ErrorCode.POLICY_VIOLATION),
    ("running", "verifying", "phases_passed", ErrorCode.POLICY_VIOLATION),
    ("verifying", "pr_ready", "verification_passed", ErrorCode.VERIFICATION_FAILED),
    ("pr_ready", "user_review", "pr_created", ErrorCode.POLICY_VIOLATION),
    ("pr_ready", "user_review", "ci_passed", ErrorCode.VERIFICATION_FAILED),
    ("user_review", "completed", "user_approved", ErrorCode.APPROVAL_REQUIRED),
    ("user_review", "completed", "merge_policy_satisfied", ErrorCode.POLICY_VIOLATION),
    ("user_review", "completed", "no_blocking_findings", ErrorCode.POLICY_VIOLATION),
])
def test_each_missing_guard_rejects_transition(source, target, field, code):
    with pytest.raises(DomainError) as exc:
        transition_task(TaskStatus(source), TaskStatus(target), replace(EVIDENCE, **{field: False}))
    assert exc.value.code is code


@pytest.mark.parametrize("source", ["approval_pending", "blocked", "waiting_for_capacity"])
def test_initial_approval_and_all_resume_paths_recheck_approvals(source):
    with pytest.raises(DomainError) as exc:
        transition_task(TaskStatus(source), TaskStatus.READY)
    assert exc.value.code is ErrorCode.APPROVAL_REQUIRED


@pytest.mark.parametrize("fake_fact", ["false", "true", 1, None, [], {}])
def test_guard_does_not_trust_truthy_external_payloads(fake_fact):
    with pytest.raises(DomainError) as exc:
        transition_task(TaskStatus.READY, TaskStatus.RUNNING,
                        replace(EVIDENCE, lease_acquired=fake_fact))
    assert exc.value.code is ErrorCode.LEASE_CONFLICT


def test_stale_version_rejects_even_with_all_evidence():
    with pytest.raises(DomainError) as exc:
        transition_task(TaskStatus.READY, TaskStatus.RUNNING,
                        replace(EVIDENCE, current_version=5))
    assert exc.value.code is ErrorCode.STALE_VERSION
    assert exc.value.retryable is True


@pytest.mark.parametrize("expected,current", [(None, 1), (1, None), (-1, -1), (True, True),
                                             ("4", 4), (4, 4.0)])
def test_partial_or_invalid_versions_are_not_treated_as_matching(expected, current):
    with pytest.raises(DomainError) as exc:
        transition_task(TaskStatus.READY, TaskStatus.RUNNING,
                        replace(EVIDENCE, expected_version=expected, current_version=current))
    assert exc.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("call,source,target", [
    (transition_task, "ready", TaskStatus.RUNNING),
    (transition_task, TaskStatus.READY, "running"),
    (transition_task, ProjectStatus.COMPLETED, TaskStatus.CANCELLED),
    (transition_project, "draft", ProjectStatus.PLANNING),
    (transition_phase_attempt, TaskStatus.RUNNING, PhaseAttemptStatus.PASSED),
])
def test_typed_boundaries_reject_strings_and_other_lifecycles(call, source, target):
    with pytest.raises(DomainError) as exc:
        call(source, target)
    assert exc.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("legacy,canonical", [
    ("in_progress", "running"), ("review_ready", "verifying"),
    ("verification_failed", "blocked"), ("feedback", "feedback_required"),
    ("done", "completed"), ("waiting_capacity", "waiting_for_capacity"),
])
def test_legacy_aliases_are_explicit_read_only_projections(legacy, canonical):
    task = Task.from_dict({"id": "TASK-001", "title": "fixture", "status": legacy})
    original = task.to_dict()
    assert task.domain_status is TaskStatus(canonical)
    assert task.to_dict() == original
    assert task.status == legacy


@pytest.mark.parametrize("status", list(TaskStatus))
def test_canonical_status_names_are_accepted_without_normalization(status):
    assert parse_task_status(status.value) is status


@pytest.mark.parametrize("status", ["", "READY", " ready ", "../../secret", "$(touch /tmp/x)",
                                    None, 42, {}, ["ready"]])
def test_unknown_status_stays_in_legacy_record_and_projection_fails_safely(status):
    task = Task.from_dict({"id": "TASK-001", "title": "fixture", "status": status})
    assert task.to_dict()["status"] == status
    with pytest.raises(DomainError) as exc:
        _ = task.domain_status
    assert exc.value.code is ErrorCode.INVALID_INPUT
    if isinstance(status, str) and status:
        assert status not in str(exc.value)


def test_transition_context_is_immutable():
    with pytest.raises(FrozenInstanceError):
        EVIDENCE.design_approved = False


@pytest.mark.parametrize("code,retryable", [
    (ErrorCode.INVALID_INPUT, False), (ErrorCode.STALE_VERSION, True),
    (ErrorCode.LEASE_CONFLICT, True), (ErrorCode.APPROVAL_REQUIRED, False),
    (ErrorCode.CAPABILITY_UNAVAILABLE, False), (ErrorCode.AUTH_REQUIRED, False),
    (ErrorCode.CAPACITY_EXHAUSTED, True), (ErrorCode.POLICY_VIOLATION, False),
    (ErrorCode.VERIFICATION_FAILED, False), (ErrorCode.EXTERNAL_TRANSIENT, True),
])
def test_public_error_contract(code, retryable):
    error = DomainError(code, "Safe public message.")
    assert error.code.value == code.name
    assert error.retryable is retryable
    assert str(error) == "Safe public message."
