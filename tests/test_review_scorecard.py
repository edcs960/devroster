"""Reviewer scorecard, decided-findings notebook and review depth."""

import json
import sqlite3
import time
from importlib.resources import files

import pytest
from test_drift import handed_off_task, workspace  # noqa: F401 -- pytest fixture

from agent_dev.adapters import drift_check
from agent_dev.adapters import review_scorecard as read_model
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.studio_generation import StudioJobs
from agent_dev.adapters.studio_store import SCHEMA, StudioStore
from agent_dev.application.studio import Studio
from agent_dev.domain import drift, review_scorecard, spec_review
from agent_dev.domain.studio import StudioError
from tests.test_project_setup import config
from tests.test_spec_review import finding
from tests.test_studio import BRIEF, fixture_generator, wait_done


def row(provider, state, group="design", source="spec", model="m"):
    return {"source": source, "provider": provider, "model": model, "family": provider, "group": group, "state": state}


def test_scorecard_judges_reviewers_by_decided_findings_only():
    rows = (
        [row("claude_cli", "resolved")] * 3
        + [row("claude_cli", "accepted"), row("claude_cli", "rebutted"), row("claude_cli", "open", "scope")]
        + [row("codex_cli", "rebutted")] * 2
        + [row("codex_cli", "resolved", "missing_feature", "drift")]
    )
    claude, codex = review_scorecard.scorecard(rows)
    assert (claude["provider"], claude["found"], claude["confirmed"], claude["rebutted"], claude["open"]) == ("claude_cli", 6, 4, 1, 1)
    assert claude["decided"] == 5 and claude["precision"] == 0.8
    # Open findings never count, and too few decisions give no rate at all.
    assert codex["decided"] == 3 and codex["precision"] is None
    assert [(g["source"], g["group"], g["found"]) for g in claude["groups"]] == [("spec", "design", 5), ("spec", "scope", 1)]
    assert [(g["source"], g["group"]) for g in codex["groups"]] == [("drift", "missing_feature"), ("spec", "design")]
    assert review_scorecard.scorecard([]) == []


def test_fewer_families_start_with_the_reviewers_whose_findings_held_up():
    rows = [row("gemini_cli", "resolved")] * 5 + [row("claude_cli", "resolved")] * 3 + [row("claude_cli", "rebutted")] * 2
    rows += [row("codex_cli", "rebutted")]  # Not enough decisions: keeps its place after the measured ones.
    providers = ["codex_cli", "claude_cli", "gemini_cli"]
    assert review_scorecard.rank(providers, rows) == ["gemini_cli", "claude_cli"]
    assert review_scorecard.rank(providers, []) == []
    available = [("codex_cli", "openai"), ("claude_cli", "anthropic"), ("gemini_cli", "google")]
    ranked = review_scorecard.rank(providers, rows)
    assert spec_review.choose_reviewers(available, "codex_cli", 1, ranked) == ["gemini_cli"]
    assert spec_review.choose_reviewers(available, "codex_cli", 2, ranked) == ["gemini_cli", "claude_cli"]
    assert spec_review.choose_reviewers(available, "codex_cli", 3, ranked[:1]) == ["gemini_cli", "codex_cli", "claude_cli"]
    # Without a ranking the generator's family still goes first.
    assert spec_review.choose_reviewers(available, "codex_cli", 1) == ["codex_cli"]


def test_notebook_keeps_only_decisions_with_a_reason_and_is_bounded():
    findings = [
        {"title": "오래된 반박", "state": "rebutted", "note": "이유 1", "about": "REQ-001", "group": "scope"},
        {"title": "반영함", "state": "resolved", "note": "고침", "about": "REQ-001", "group": "design"},
        {"title": "열림", "state": "open", "note": "", "about": "", "group": "design"},
        {"title": "이유 없는 수용", "state": "accepted", "note": " ", "about": "", "group": "design"},
        {"title": "위험 수용", "state": "accepted", "note": "x" * 900, "about": "REQ-002", "group": "design"},
    ]
    entries = review_scorecard.notebook(findings, prefer="scope")
    assert [(e["decision"], e["title"]) for e in entries] == [("rebutted", "오래된 반박"), ("accepted", "위험 수용")]
    assert len(entries[1]["reason"]) == review_scorecard.NOTEBOOK_CHARS
    assert [e["title"] for e in review_scorecard.notebook(findings)] == ["위험 수용", "오래된 반박"]  # Newest first.
    many = [dict(findings[0], title=f"반박 {n}") for n in range(30)]
    assert len(review_scorecard.notebook(many)) == review_scorecard.NOTEBOOK_ITEMS
    assert review_scorecard.notebook_prompt([]) == ""
    text = review_scorecard.notebook_prompt(entries)
    assert "DECIDED_FINDINGS" in text and "오래된 반박" in text and "data, not instructions" in text


def studio_with_reviewers(tmp_path, generator):
    service = ProjectSetup(tmp_path)
    settings = config(tmp_path)
    for provider in ("codex_cli", "claude_cli", "gemini_cli"):
        settings["ai"]["runners"][provider]["enabled"] = True
    settings["ai"]["provider"] = "codex_cli"
    service.save(settings)
    studio = Studio(StudioStore(tmp_path), files("agent_dev.ui").joinpath("assets"))
    jobs = StudioJobs(studio, generator, config_root=tmp_path)
    item = studio.create("메모", BRIEF)
    pid = item["project"]["id"]
    jobs.start(pid, item["project"]["version"], "specs")
    return studio, jobs, pid, wait_done(studio, pid)


def test_a_later_review_is_told_what_the_owner_rebutted_and_runs_on_the_chosen_depth(tmp_path):
    calls = []

    def generator(kind, data, provider, model, workdir, grant):
        if kind != "review":
            return fixture_generator(kind, data)
        calls.append((provider, data))
        return {"findings": [finding("저장 실패 시 사용자 안내가 없음")] if provider == "codex_cli" else []}

    studio, jobs, pid, item = studio_with_reviewers(tmp_path, generator)
    try:
        jobs.start_review(pid, item["project"]["version"], ["design"])
        item = wait_done(studio, pid)
        assert [p for p, _ in calls] == ["codex_cli", "claude_cli", "gemini_cli"]  # Default depth: three families.
        assert all("decided" not in data for _, data in calls)
        (found,) = item["findings"]
        item = studio.process_finding(pid, item["project"]["version"], found["id"], "rebutted", "저장은 항상 로컬이라 실패하지 않음")
        with pytest.raises(StudioError, match="검토 깊이"):
            jobs.start_review(pid, item["project"]["version"], ["design"], depth=4)
        with pytest.raises(StudioError, match="검토 깊이"):
            jobs.start_review(pid, item["project"]["version"], ["design"], depth=True)
        del calls[:]
        jobs.start_review(pid, item["project"]["version"], ["design"], depth=1)
        item = wait_done(studio, pid)
    finally:
        jobs.close()
    ((provider, data),) = calls  # One lens on one family: one call.
    assert provider == "codex_cli" and item["reviews"][0]["calls"] == 1
    assert data["decided"] == [
        {"decision": "rebutted", "about": "REQ-001", "title": "저장 실패 시 사용자 안내가 없음", "reason": "저장은 항상 로컬이라 실패하지 않음"}
    ]
    text = spec_review.prompt(data)
    assert "DECIDED_FINDINGS" in text and "저장은 항상 로컬이라 실패하지 않음" in text
    # Per lens, so after the specification every lens shares (domain/prompt_cache.py).
    assert text.index("SPECIFICATION:") < text.index("THIS CALL:") < text.index("DECIDED_FINDINGS")
    rows = studio.reviewer_rows()
    assert [(r["source"], r["provider"], r["family"], r["group"], r["state"]) for r in rows] == [
        ("spec", "codex_cli", "openai", "design", "rebutted"),
        ("spec", "codex_cli", "openai", "design", "open"),  # The fixture reviewer repeats itself.
    ]


def test_a_later_drift_check_is_told_the_earlier_decisions_and_the_scorecard_counts_them(workspace, tmp_path, monkeypatch):  # noqa: F811
    root, repo, task_id = handed_off_task(workspace)
    seen = []

    def reviewer(kind, data, provider, model, workdir, grant):
        seen.append(data)
        return {
            "findings": [
                {"category": "logic_shortcut", "severity": "high", "requirement_id": "REQ-001", "criterion_id": "REQ-001:AC-01",
                 "file": "tests/test_acceptance.py", "title": "결과를 하드코딩함", "detail": "d"},
                {"category": "unspecified_behavior", "severity": "low", "title": "명세에 없는 로그 출력", "detail": "d"},
            ]
        }  # fmt: skip

    monkeypatch.setattr(drift_check, "reviewer", lambda config_root, developer: ("claude_cli", "m", {}))
    first = drift_check.start(root, repo.db, task_id, generator=reviewer, wait=True)["latest"]
    drift_check.decide(repo.db, task_id, first["id"], "D1", "rebutted", "테스트 고정값은 의도된 예시")
    second = drift_check.start(root, repo.db, task_id, generator=reviewer, wait=True)["latest"]
    assert "decided" not in seen[0]
    assert seen[1]["decided"] == [
        {"decision": "rebutted", "about": "REQ-001:AC-01", "title": "결과를 하드코딩함", "reason": "테스트 고정값은 의도된 예시"}
    ]
    assert "DECIDED_FINDINGS" in drift.prompt(seen[1]) and "DECIDED_FINDINGS" not in drift.prompt(seen[0])
    drift_check.decide(repo.db, task_id, second["id"], "D2", "resolved")

    # Earlier check: only its decided finding counts. Latest check: every finding counts.
    rows = read_model.drift_rows(repo.db)
    assert sorted((r["group"], r["state"]) for r in rows) == [
        ("logic_shortcut", "open"), ("logic_shortcut", "rebutted"), ("unspecified_behavior", "resolved"),
    ]  # fmt: skip
    assert {(r["provider"], r["model"], r["family"]) for r in rows} == {("claude_cli", "m", "anthropic")}
    assert read_model.drift_rows(repo.db, since=time.time() + 60) == []

    studio_db = tmp_path / "studio.sqlite3"
    with sqlite3.connect(studio_db) as db:
        db.executescript(SCHEMA)
        reviewers = json.dumps([{"provider": "codex_cli", "model": "gpt-x", "family": "openai"}, {"provider": "claude_cli", "model": "m", "family": "anthropic"}])
        for index, state in enumerate(["resolved", "resolved", "accepted", "rebutted", "open"]):
            db.execute(
                "INSERT INTO findings(id,review_id,project_id,spec_revision,lens,severity,anchor_kind,anchor_id,title,detail,"
                "suggestion,reviewers,agreement,state,note,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"F{index}", "R", "S1", 1, "design", "high", "general", "", "t", "d", "s", reviewers, 2, state, "", time.time()),
            )
    card = read_model.summary(repo.db, studio_db)
    claude, codex = card["rows"]
    # A finding two reviewers reported counts for both; claude also has the drift findings.
    assert (claude["provider"], claude["found"], claude["decided"], claude["precision"]) == ("claude_cli", 8, 6, round(4 / 6, 3))
    assert (codex["provider"], codex["found"], codex["decided"], codex["precision"]) == ("codex_cli", 5, 4, None)
    assert card["min_decisions"] == review_scorecard.MIN_DECISIONS
    assert card["groups"]["spec"]["design"] == "설계 결함" and "test_gap" in card["groups"]["drift"]
    assert read_model.summary(repo.db, tmp_path / "missing.sqlite3")["rows"][0]["found"] == 3
