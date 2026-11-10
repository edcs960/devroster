"""Specs in two stages (requirements, then four document groups) and per-document regeneration."""

import pytest
from test_studio import BRIEF, fixture_generator, specifications, studio, wait_done  # noqa: F401

from agent_dev.adapters.studio_generation import StudioJobs, prompt_for, staged_specs
from agent_dev.domain.studio import DOCUMENT_GROUPS, DOCUMENTS, StudioError, validate_specs


class Recorder:
    """Fixture generator that records calls and can fail one document group."""

    def __init__(self, fail_group=None, fail_times=2):
        self.calls, self.fail_group, self.fail_times = [], fail_group, fail_times

    def __call__(self, kind, data, *rest):
        self.calls.append((kind, tuple(data.get("documents_to_write", ()))))
        if kind == "spec_documents" and self.fail_group and tuple(data["documents_to_write"]) == self.fail_group and self.fail_times:
            self.fail_times -= 1
            return {"documents": {"엉뚱한문서": "# x"}}
        return fixture_generator(kind, data, *rest)


def run(generator, data=None):
    call = lambda kind, value: generator(kind, value)
    return staged_specs(data or {"brief": BRIEF, "title": "메모"}, call)


def test_requirements_first_then_each_document_group_once():
    recorder = Recorder()
    payload = run(recorder)
    assert [k for k, _ in recorder.calls] == ["spec_requirements"] + ["spec_documents"] * len(DOCUMENT_GROUPS)
    assert [names for kind, names in recorder.calls[1:]] == [names for _, names in DOCUMENT_GROUPS]
    payload["documents"]["개발기획서"] = "# 원본 개발기획서\n\n" + BRIEF
    assert set(validate_specs(payload, BRIEF)["documents"]) == set(DOCUMENTS)
    assert payload["failed_documents"] == []


def test_a_group_is_retried_once_then_left_for_regeneration():
    group = DOCUMENT_GROUPS[3][1]  # includes the traceability matrix
    retried = run(Recorder(group, fail_times=1))
    assert retried["failed_documents"] == []
    failed = run(Recorder(group, fail_times=2))
    assert failed["failed_documents"] == list(group)
    assert "다시 생성" in failed["documents"]["산출물목록"]
    assert "REQ-001" in failed["documents"]["요구사항추적성매트릭스"]  # still a valid spec
    failed["documents"]["개발기획서"] = "# 원본 개발기획서\n\n" + BRIEF
    validate_specs(failed, BRIEF)


def test_document_prompt_names_only_the_group_and_confirmed_ids():
    prompt = prompt_for("spec_documents", {"brief": BRIEF, "documents_to_write": ["기술설계서"], "spec": {"requirements": []}})
    assert "write ONLY these specification documents" in prompt and "traceability matrix" not in prompt
    assert "Step 1 of 2" in prompt_for("spec_requirements", {"brief": BRIEF})


def test_one_document_is_regenerated_into_a_new_revision(studio):  # noqa: F811
    data = studio.create("메모", BRIEF)
    project = data["project"]["id"]
    jobs = StudioJobs(studio, fixture_generator)
    jobs.start(project, data["project"]["version"], "specs")
    first = wait_done(studio, project)
    before = first["spec"]["payload"]["documents"]

    def rewrite(kind, value, *rest):
        assert kind == "spec_documents" and value["documents_to_write"] == ["기술설계서"]
        assert value["user_note"] == "캐시 전략을 추가" and "기술설계서" in value["current_documents"]
        return {"documents": {"기술설계서": "# 기술설계서\n\n" + "캐시 전략을 포함한 새 기술설계 내용입니다. " * 4}}

    jobs.generator = rewrite
    jobs.start(project, first["project"]["version"], "spec_documents", documents=["기술설계서"], note="캐시 전략을 추가")
    second = wait_done(studio, project)
    assert second["jobs"][0]["state"] == "done" and second["spec"]["revision"] == first["spec"]["revision"] + 1
    after = second["spec"]["payload"]["documents"]
    assert "캐시 전략" in after["기술설계서"]
    assert {n: after[n] for n in after if n != "기술설계서"} == {n: before[n] for n in before if n != "기술설계서"}
    with pytest.raises(StudioError):
        jobs.start(project, second["project"]["version"], "spec_documents", documents=["개발기획서"])
    jobs.close()
