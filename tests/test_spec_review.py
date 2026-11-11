"""Independent specification review: fresh reviewers per lens and model family, tracked findings."""

import json

import pytest

from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.studio_generation import StudioJobs
from agent_dev.domain import spec_review
from agent_dev.domain.studio import StudioError
from tests.test_project_setup import config
from tests.test_studio import BRIEF, fixture_generator, specifications, wait_done

SPEC = specifications()
KNOWN = spec_review.anchors(SPEC)


def finding(title, anchor=("requirement", "REQ-001"), severity="high"):
    return {"severity": severity, "anchor": {"kind": anchor[0], "id": anchor[1]}, "title": title, "detail": "d", "suggestion": "s"}


def test_parse_keeps_known_anchors_and_falls_back_to_general():
    rows = spec_review.parse(
        {"findings": [finding("저장 실패 처리 없음"), finding("없는 요구사항", ("requirement", "REQ-999")), {"title": ""}]},
        KNOWN,
    )
    assert [(r["anchor_kind"], r["anchor_id"]) for r in rows] == [("requirement", "REQ-001"), ("general", "")]
    with pytest.raises(StudioError):
        spec_review.parse({"items": []}, KNOWN)


def test_merge_marks_problems_two_model_families_found_independently():
    results = [
        {"lens": "design", "family": "openai", "provider": "codex_cli", "findings": spec_review.parse({"findings": [finding("저장 실패 시 처리가 없음", severity="medium")]}, KNOWN)},
        {"lens": "design", "family": "anthropic", "provider": "claude_cli", "findings": spec_review.parse({"findings": [finding("저장 실패 처리 없음"), finding("목록 정렬 기준 없음", severity="low")]}, KNOWN)},
        {"lens": "readiness", "family": "anthropic", "provider": "claude_cli", "findings": spec_review.parse({"findings": [finding("저장 실패 처리 없음")]}, KNOWN)},
    ]
    merged = spec_review.merge(results)
    top = merged[0]
    assert top["lens"] == "design" and top["agreement"] == 2 and top["families"] == ["anthropic", "openai"]
    assert top["severity"] == "high"  # The most severe rating wins.
    assert len(merged) == 3  # Same words under another lens stay a separate finding.


def test_gates_lenses_and_reviewer_choice():
    rows = [
        {"state": "open", "severity": "high", "lens": "design"},
        {"state": "open", "severity": "high", "lens": "mockup"},
        {"state": "accepted", "severity": "high", "lens": "scope"},
        {"state": "open", "severity": "medium", "lens": "readiness"},
    ]
    assert [r["lens"] for r in spec_review.blocking(rows, "spec")] == ["design"]
    assert [r["lens"] for r in spec_review.blocking(rows, "handoff")] == ["design", "mockup"]
    with pytest.raises(StudioError):
        spec_review.lenses_for(["mockup"], has_mock=False)
    available = [("codex_cli", "openai"), ("openai_api", "openai"), ("claude_cli", "anthropic"), ("gemini_cli", "google"), ("exaone_api", "exaone")]
    assert spec_review.choose_reviewers(available, "claude_cli") == ["claude_cli", "codex_cli", "gemini_cli"]


def test_reviewer_prompt_carries_only_the_document():
    data = spec_review.review_input(SPEC, None, "design", "Korean")
    text = spec_review.prompt(data)
    assert "no memory of how this specification was written" in text and "REQ-001" in text
    assert "brief" not in json.dumps(data["specification"]) or "source_quote" in json.dumps(data["specification"])


def review_generator(kind, data, provider, model, workspace, grant):
    if kind != "review":
        return fixture_generator(kind, data)
    grant["usage_sink"](provider, model or provider + "-model", {"input_tokens": 100, "output_tokens": 20})
    if provider == "gemini_cli":
        raise StudioError("reviewer failed")  # One failing reviewer does not discard the others.
    if data["lens"] == "design":
        return {"findings": [finding("저장 실패 시 사용자 안내가 없음")]}
    return {"findings": [finding("용어 '메모'의 범위가 모호함", ("requirement", "REQ-001"), "medium")] if provider == "claude_cli" else []}


def test_review_job_tracks_findings_and_gates_approval(tmp_path):
    from importlib.resources import files

    from agent_dev.adapters.studio_store import StudioStore
    from agent_dev.application.studio import Studio

    service = ProjectSetup(tmp_path)
    settings = config(tmp_path)
    for provider in ("codex_cli", "claude_cli", "gemini_cli"):
        settings["ai"]["runners"][provider]["enabled"] = True
    settings["ai"]["provider"] = "codex_cli"
    service.save(settings)
    studio = Studio(StudioStore(tmp_path), files("agent_dev.ui").joinpath("assets"))
    jobs = StudioJobs(studio, review_generator, config_root=tmp_path)
    try:
        item = studio.create("메모", BRIEF)
        pid = item["project"]["id"]
        jobs.start(pid, item["project"]["version"], "specs")
        item = wait_done(studio, pid)
        for question in item["spec"]["payload"]["questions"]:
            question["answer"] = "답변"
        item = studio.save_specs(pid, item["project"]["version"], item["spec"]["payload"])
        jobs.start_review(pid, item["project"]["version"], ["design", "comprehension"])
        item = wait_done(studio, pid)
    finally:
        jobs.close()
    assert item["jobs"][0]["state"] == "done", item["jobs"][0]
    (review,) = item["reviews"]
    assert review["failed"] == 2 and review["calls"] == 6  # 2 lenses x 3 families, gemini failed twice.
    findings = item["findings"]
    design = next(f for f in findings if f["lens"] == "design")
    assert design["agreement"] == 2 and design["severity"] == "high" and design["state"] == "open"
    with pytest.raises(StudioError, match="심각한 지적 1건"):
        studio.approve_specs(pid, item["project"]["version"])
    with pytest.raises(StudioError):
        studio.process_finding(pid, item["project"]["version"], design["id"], "accepted", "")  # A reason is required.
    item = studio.process_finding(pid, item["project"]["version"], design["id"], "accepted", "1차 범위에서 제외")
    item = studio.approve_specs(pid, item["project"]["version"])
    approved = next(json.loads(e["payload"]) for e in item["events"] if e["kind"] == "spec.approved")
    assert [f["id"] for f in approved["accepted_findings"]] == [design["id"]]
    # Every reviewer call is in the usage record, failed ones included.
    import sqlite3

    with sqlite3.connect(studio.store.path) as db:
        steps = [json.loads(r[0])["step"] for r in db.execute("SELECT payload FROM events WHERE kind='job.usage'")]
    assert steps.count("review:design") == 3 and steps.count("review:comprehension") == 3


def test_open_mockup_findings_block_the_development_handoff(tmp_path):
    import sqlite3
    import time
    from importlib.resources import files

    from agent_dev.adapters import studio_handoff
    from agent_dev.adapters.studio_store import StudioStore
    from agent_dev.application.studio import Studio
    from agent_dev.domain.errors import DomainError

    studio = Studio(StudioStore(tmp_path), files("agent_dev.ui").joinpath("assets"))
    jobs = StudioJobs(studio, fixture_generator)
    try:
        item = studio.create("메모", BRIEF)
        pid = item["project"]["id"]
        jobs.start(pid, item["project"]["version"], "specs")
        item = wait_done(studio, pid)
        for question in item["spec"]["payload"]["questions"]:
            question["answer"] = "답변"
        item = studio.save_specs(pid, item["project"]["version"], item["spec"]["payload"])
        item = studio.approve_specs(pid, item["project"]["version"])
        jobs.start(pid, item["project"]["version"], "mock")
        item = wait_done(studio, pid)
    finally:
        jobs.close()
    with sqlite3.connect(studio.store.path) as db:
        db.execute("INSERT INTO reviews VALUES('R1',?,?,'[]','[]',1,0,?)", (pid, item["spec"]["revision"], time.time()))
        db.execute(
            "INSERT INTO findings(id,review_id,project_id,spec_revision,lens,severity,anchor_kind,anchor_id,title,detail,"
            "suggestion,reviewers,agreement,state,created) VALUES('F1','R1',?,?,'mockup','high','screen','SCR-001',"
            "'화면에 저장 버튼 없음','d','s','[]',1,'open',?)",
            (pid, item["spec"]["revision"], time.time()),
        )
    with sqlite3.connect(studio.store.path) as db, pytest.raises(DomainError, match="개발 인계"):
        db.row_factory = sqlite3.Row
        studio_handoff.review_gate(db, pid)
    with sqlite3.connect(studio.store.path) as db:
        db.row_factory = sqlite3.Row
        # Traceability reads the same bundle to compare the source; a finding must not fail it.
        try:
            studio_handoff._read_bundle(db, pid)
        except DomainError as error:  # Unapproved screens may still stop the bundle itself.
            assert "독립 검토" not in str(error)
    studio.process_finding(pid, studio.store.detail(pid)["project"]["version"], "F1", "resolved", "버튼 추가")
    with sqlite3.connect(studio.store.path) as db:
        db.row_factory = sqlite3.Row
        studio_handoff.review_gate(db, pid)  # No longer blocked.
