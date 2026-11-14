"""Drift check: a task's approved requirements against its scoped diff, in both directions."""

import json
from typing import Any, cast

import pytest
from test_project_journey import prepared
from test_project_journey import workspace as journey_workspace

from agent_dev.adapters import drift_check
from agent_dev.domain import criteria_tests, drift
from agent_dev.domain.errors import DomainError


@pytest.fixture
def workspace(tmp_path):
    return cast(Any, journey_workspace).__wrapped__(tmp_path)


def handed_off_task(workspace):
    from pathlib import Path

    from agent_dev.adapters.workflow_runtime import WorkflowRuntime

    root, repo, _source, _target = workspace
    profile = json.loads(repo.db.rows("SELECT content_json FROM project_profile_versions")[0]["content_json"])
    profile["phases"] = [{"id": "developer", "capabilities": ["code.write"]}]
    repo.revise_profile("P1", profile, 0)
    first, _imported = prepared(workspace)
    task_id = first["selection"]["tasks"][0]["id"]
    runtime = WorkflowRuntime(root)
    runtime.store.create_snapshot(task_id, runtime.resolve_snapshot(task_id))
    binding = next(iter(runtime.store.snapshot(task_id)["resolved"]["repositories"].values()))
    (Path(binding["worktree"]) / "tests/test_acceptance.py").write_text(
        "def test_acceptance():\n    assert 'hardcoded' == 'hardcoded'\n", encoding="utf-8"
    )
    return root, repo, task_id


def test_parse_keeps_known_references_only():
    rows = drift.parse(
        {
            "findings": [
                {"category": "logic_shortcut", "severity": "high", "requirement_id": "REQ-001", "criterion_id": "REQ-001:AC-01", "file": "a.py", "title": "하드코딩", "detail": "d"},
                {"category": "unknown", "requirement_id": "REQ-999", "criterion_id": "X", "file": "zzz.py", "title": "기타"},
                {"title": ""},
            ]
        },
        {"REQ-001"},
        {"REQ-001:AC-01"},
        {"a.py"},
    )
    assert [r["id"] for r in rows] == ["D1", "D2"]
    assert rows[1]["category"] == "missing_feature" and rows[1]["requirement_id"] == "" and rows[1]["file"] == ""
    with pytest.raises(DomainError):
        drift.parse({"nope": 1}, set(), set(), set())


def test_view_applies_decisions_to_the_latest_check_only():
    checks = [
        {"id": "C1", "findings": [{"id": "D1", "severity": "high", "category": "missing_feature"}]},
        {"id": "C2", "findings": [{"id": "D1", "severity": "high", "category": "unspecified_behavior"}]},
    ]
    view = drift.view(checks, [{"check_id": "C1", "finding_id": "D1", "state": "accepted"}])
    assert view["id"] == "C2" and view["findings"][0]["state"] == "open" and view["open_high"] == 1 and view["reverse"] == 1


def test_drift_check_compares_requirements_with_the_diff(workspace, monkeypatch):
    root, repo, task_id = handed_off_task(workspace)
    seen = {}

    def reviewer(kind, data, provider, model, workdir, grant):
        seen.update(kind=kind, data=data, provider=provider)
        grant["usage_sink"](provider, model or "reviewer-model", {"input_tokens": 500, "output_tokens": 50})
        return {
            "findings": [
                {"category": "logic_shortcut", "severity": "high", "requirement_id": "REQ-001", "criterion_id": "REQ-001:AC-01",
                 "file": "tests/test_acceptance.py", "title": "결과를 하드코딩함", "detail": "실제 계산 대신 고정값을 비교합니다."},
                {"category": "unspecified_behavior", "severity": "low", "title": "명세에 없는 로그 출력", "detail": "d"},
            ]
        }

    monkeypatch.setattr(drift_check, "reviewer", lambda config_root, developer: ("claude_cli", "m", {}))
    result = drift_check.start(root, repo.db, task_id, generator=reviewer, wait=True)
    assert seen["kind"] == "drift" and seen["data"]["requirements"][0]["id"] == "REQ-001"
    assert seen["data"]["requirements"][0]["acceptance"][0]["id"] == "REQ-001:AC-01"
    assert "tests/test_acceptance.py" in seen["data"]["diff"] and "hardcoded" in seen["data"]["diff"]
    latest = result["latest"]
    assert latest["open_high"] == 1 and latest["reverse"] == 1 and not result["running"]
    high = latest["findings"][0]
    with pytest.raises(DomainError):
        drift_check.decide(repo.db, task_id, latest["id"], high["id"], "rebutted", "")
    after = drift_check.decide(repo.db, task_id, latest["id"], high["id"], "rebutted", "테스트 고정값은 의도된 예시")
    assert after["latest"]["open_high"] == 0
    usage = [json.loads(r["payload_json"]) for r in repo.db.rows("SELECT payload_json FROM events WHERE kind='RunnerUsage' AND aggregate_id=?", (task_id,))]
    assert any(u["phase_id"] == "drift" and u["usage"] == {"input_tokens": 500, "output_tokens": 50} for u in usage)


def test_criteria_carry_their_named_test_and_unnamed_ones_are_counted():
    reqs = [{"id": "REQ-001", "acceptance": [{"id": "REQ-001:AC-01", "text": "a"}, {"id": "REQ-001:AC-02", "text": "b"}]}]
    named = criteria_tests.attach(reqs, {"REQ-001:AC-01": "tests/test_a.py::test_a", "REQ-009:AC-01": "x", "REQ-001:AC-02": 7})
    assert [c["test"] for c in named[0]["acceptance"]] == ["tests/test_a.py::test_a", ""]
    assert criteria_tests.coverage(named) == {"total": 2, "named": 1, "unnamed": ["REQ-001:AC-02"]}
    assert criteria_tests.coverage(criteria_tests.attach(reqs, None)) == {"total": 2, "named": 0, "unnamed": ["REQ-001:AC-01", "REQ-001:AC-02"]}
    assert "test" not in reqs[0]["acceptance"][0]
    text = drift.prompt({"language": "Korean", "requirements": named, "diff": ""})
    assert "test_gap" in text and "tests/test_a.py::test_a" in text
    rows = drift.parse(
        {"findings": [{"category": "test_gap", "severity": "high", "criterion_id": "REQ-001:AC-01", "file": "tests/test_a.py", "title": "상수 비교"}]},
        {"REQ-001"}, {"REQ-001:AC-01"}, {"tests/test_a.py"},
    )
    assert rows[0]["category"] == "test_gap" and rows[0]["requirement_id"] == "REQ-001"


def test_the_reviewer_and_the_screen_see_which_test_checks_each_criterion(workspace, monkeypatch):
    root, repo, task_id = handed_off_task(workspace)
    before = drift_check.status(repo.db, task_id)["criteria"]
    assert before["total"] >= 1 and before["named"] == 0 and "REQ-001:AC-01" in before["unnamed"]
    repo.event(task_id, "CriteriaTestsNamed", {"node_id": "test", "criteria_tests": {"REQ-001:AC-01": "tests/test_acceptance.py::test_acceptance"}})
    seen = {}

    def reviewer(kind, data, provider, model, workdir, grant):
        seen.update(data=data)
        return {
            "findings": [
                {"category": "test_gap", "severity": "high", "criterion_id": "REQ-001:AC-01", "file": "tests/test_acceptance.py",
                 "title": "테스트가 상수끼리 비교함", "detail": "수용 기준이 지켜지지 않아도 통과합니다."}
            ]
        }

    monkeypatch.setattr(drift_check, "reviewer", lambda config_root, developer: ("claude_cli", "m", {}))
    result = drift_check.start(root, repo.db, task_id, generator=reviewer, wait=True)
    assert seen["data"]["requirements"][0]["acceptance"][0]["test"] == "tests/test_acceptance.py::test_acceptance"
    assert result["criteria"]["named"] == 1 and "REQ-001:AC-01" not in result["criteria"]["unnamed"]
    finding = result["latest"]["findings"][0]
    assert finding["category"] == "test_gap" and result["categories"]["test_gap"] and result["latest"]["open_high"] == 1


def test_a_failed_reviewer_is_reported_without_provider_output(workspace, monkeypatch):
    root, repo, task_id = handed_off_task(workspace)

    def broken(*_args):
        raise RuntimeError("raw provider output with secrets")

    monkeypatch.setattr(drift_check, "reviewer", lambda config_root, developer: ("codex_cli", "m", {}))
    result = drift_check.start(root, repo.db, task_id, generator=broken, wait=True)
    assert result["failure"] and "secrets" not in result["failure"] and result["latest"] is None


def test_reviewer_prefers_another_model_family(tmp_path, monkeypatch):
    from agent_dev.adapters import project_setup

    monkeypatch.setattr(
        project_setup,
        "ai_defaults",
        lambda root: {"provider": "codex_cli", "runners": {"codex_cli": {"enabled": True}, "claude_cli": {"enabled": True}, "gemini_cli": {"enabled": False}}},
    )
    import agent_dev.adapters.studio_generation as generation

    monkeypatch.setattr(generation, "runner_config", lambda root, ai, provider, model=None, effort=None: {"model": "", "effort": "", "routing": {"mode": "fixed"}})
    assert drift_check.reviewer(tmp_path, "codex_cli")[0] == "claude_cli"
    assert drift_check.reviewer(tmp_path, "claude_cli")[0] == "codex_cli"


def test_gate_counts_a_check_only_for_the_verified_code():
    clear = {"evidence": "E1", "open_high": 0}
    blocked = {"evidence": "E1", "open_high": 2}
    assert drift.gate("off", True, None, False, False, "E1") == {"mode": "off", "state": "off", "blocks": False, "message": ""}
    assert drift.gate("block", False, None, False, False, "E1")["state"] == "not_applicable"
    assert not drift.gate("block", False, None, False, False, "E1")["blocks"]
    for latest, running, failed, evidence, state in (
        (None, False, False, "E1", "missing"),
        (None, True, False, "E1", "running"),
        (None, False, True, "E1", "failed"),
        (clear, False, False, "E2", "stale"),
        (clear, False, False, "", "stale"),  # Nothing verified yet: no check can count.
        ({"open_high": 0}, False, False, "E1", "stale"),  # A check from before evidence was recorded.
        (blocked, False, False, "E1", "open"),
    ):
        result = drift.gate("block", True, latest, running, failed, evidence)
        assert (result["state"], result["blocks"]) == (state, True) and result["message"]
        assert not drift.gate("advise", True, latest, running, failed, evidence)["blocks"]
    assert "2건" in drift.gate("block", True, blocked, False, False, "E1")["message"]
    # A current clear check wins over an older failure.
    assert drift.gate("block", True, clear, False, True, "E1") == {
        "mode": "block", "state": "clear", "blocks": False, "message": drift.GATE_MESSAGES["clear"],
    }
    assert drift.gate_mode({"drift": {"gate": "advise"}}) == "advise"
    assert drift.gate_mode({"drift": {"gate": "nope"}}) == drift.gate_mode({}) == "off"


@pytest.fixture
def runtime(tmp_path):
    from test_workflow_runtime import runtime as fixture

    return cast(Any, fixture).__wrapped__(tmp_path)


def gated(runtime, monkeypatch, mode, findings):
    """Task G approved under the drift gate ``mode``, with one linked requirement and a canned reviewer."""
    import agent_dev.adapters.studio_generation as generation

    profile = json.loads(runtime.store.db.rows("SELECT content_json FROM project_profile_versions")[0]["content_json"])
    runtime.store.revise_profile("P", dict(profile, drift={"gate": mode}), 0)
    runtime.store.create_plan("P", "GATED", "D", 1, [{"id": "G", "title": "Gated", "system_name": "Topic"}])
    runtime.store.approve("plan", "GATED", 1, "user")
    runtime.store.create_snapshot("G", runtime.resolve_snapshot("G"))
    calls = []

    def reviewer(kind, data, provider, model, workdir, grant):
        calls.append(data)
        return {"findings": findings}

    requirement = {"id": "REQ-001", "title": "t", "description": "d", "acceptance": [{"id": "REQ-001:AC-01", "text": "a"}]}
    monkeypatch.setattr(drift_check, "requirements", lambda db, task_id: [requirement])
    monkeypatch.setattr(drift_check, "reviewer", lambda config_root, developer: ("claude_cli", "m", {}))
    monkeypatch.setattr(generation, "cli_generate", reviewer)
    return calls


HIGH = {"category": "criterion_unmet", "severity": "high", "requirement_id": "REQ-001", "title": "기준 미충족", "detail": "d"}


def test_block_mode_checks_after_verification_and_holds_the_publish(runtime, monkeypatch):
    from pathlib import Path

    from test_workflow_runtime import Runner

    from agent_dev.adapters.publishing import PublishAdapter
    from agent_dev.domain.errors import ErrorCode

    calls = gated(runtime, monkeypatch, "block", [HIGH])
    runtime.execute("G", "run", runner=Runner())
    result = runtime.execute("G", "verify", verify=True)
    assert result["passed"] and result["drift"]["state"] == "open" and result["drift"]["blocks"] and len(calls) == 1
    with pytest.raises(DomainError) as error:
        PublishAdapter(runtime).publish("G", "source", "dry", dry_run=True)
    assert error.value.code == ErrorCode.VERIFICATION_FAILED and "드리프트" in str(error.value)
    latest = drift_check.status(runtime.store.db, "G")["latest"]
    after = drift_check.decide(runtime.store.db, "G", latest["id"], "D1", "accepted", "다음 작업에서 처리")
    assert after["gate"]["state"] == "clear" and not after["gate"]["blocks"]
    assert PublishAdapter(runtime).publish("G", "source", "dry", dry_run=True)["dry_run"]
    # The same request replayed does not pay for a second review of the same code.
    assert runtime.execute("G", "verify", verify=True)["drift"]["state"] == "clear" and len(calls) == 1
    # Code changed after the check: the old check no longer counts once it is verified again.
    worktree = Path(runtime.store.snapshot("G")["resolved"]["repositories"]["source"]["worktree"])
    (worktree / "source.txt").write_text("changed again", encoding="utf-8")
    run = runtime.store.db.rows("SELECT id FROM verification_runs")[0]["id"]
    with runtime.store.db.transaction() as connection:
        connection.execute(
            "UPDATE verification_runs SET policy_digest=? WHERE id=?",
            (drift_check.evidence(runtime.store.snapshot("G")["resolved"]), run),
        )
    assert drift_check.status(runtime.store.db, "G")["gate"]["state"] == "stale"
    with pytest.raises(DomainError):
        drift_check.require_clear(runtime.store.db, "G")


def test_advise_mode_checks_but_never_blocks_and_off_never_calls(runtime, monkeypatch):
    from test_workflow_runtime import Runner

    from agent_dev.adapters.publishing import PublishAdapter

    calls = gated(runtime, monkeypatch, "advise", [HIGH])
    runtime.execute("G", "run", runner=Runner())
    result = runtime.execute("G", "verify", verify=True)
    assert result["drift"] == {"state": "open", "blocks": False, "message": result["drift"]["message"]} and len(calls) == 1
    assert PublishAdapter(runtime).publish("G", "source", "dry", dry_run=True)["dry_run"]


def test_without_the_setting_verification_calls_no_reviewer(runtime, monkeypatch):
    from test_workflow_runtime import Runner

    import agent_dev.adapters.studio_generation as generation

    monkeypatch.setattr(generation, "cli_generate", lambda *args: pytest.fail("no AI call without the setting"))
    runtime.execute("T", "run", runner=Runner())
    result = runtime.execute("T", "verify", verify=True)
    assert result["passed"] and "drift" not in result
    assert drift_check.status(runtime.store.db, "T")["gate"]["state"] == "off"


def test_a_reviewer_failure_blocks_only_in_block_mode_and_never_fails_verification(runtime, monkeypatch):
    from test_workflow_runtime import Runner

    import agent_dev.adapters.studio_generation as generation

    gated(runtime, monkeypatch, "block", [])

    def broken(*_args):
        raise RuntimeError("raw provider output")

    monkeypatch.setattr(generation, "cli_generate", broken)
    runtime.execute("G", "run", runner=Runner())
    result = runtime.execute("G", "verify", verify=True)
    assert result["passed"] and result["drift"]["state"] == "failed" and result["drift"]["blocks"]
    assert "raw provider" not in result["drift"]["message"]
    # A task without linked requirements has nothing to compare: the gate does not apply.
    monkeypatch.setattr(drift_check, "requirements", lambda db, task_id: [])
    assert drift_check.require_clear(runtime.store.db, "G")["state"] == "not_applicable"
