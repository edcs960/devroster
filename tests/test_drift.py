"""Drift check: a task's approved requirements against its scoped diff, in both directions."""

import json
from typing import Any, cast

import pytest
from test_project_journey import prepared
from test_project_journey import workspace as journey_workspace

from agent_dev.adapters import drift_check
from agent_dev.domain import drift
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
