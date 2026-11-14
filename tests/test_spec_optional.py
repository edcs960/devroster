"""Required and optional specification documents, and specs saved under the earlier names."""

import io
from zipfile import ZipFile

import pytest
from test_studio import BRIEF, fixture_generator, mockup, specifications, studio, wait_done  # noqa: F401

from agent_dev.adapters.studio_generation import (
    StudioJobs,
    document_input,
    prompt_for,
    regenerate_documents,
    staged_specs,
)
from agent_dev.domain import spec_review
from agent_dev.domain.studio import (
    DOCUMENTS,
    OPTIONAL_DOCUMENTS,
    OPTIONAL_GROUPS,
    RENAMED,
    REQUIRED_DOCUMENTS,
    REQUIRED_GROUPS,
    StudioError,
    current_names,
    skipped_document,
    validate_specs,
)

DETAIL, MANAGEMENT = (names for _, _, names in OPTIONAL_GROUPS)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, data, *rest):
        self.calls.append((kind, data))
        return fixture_generator(kind, data, *rest)


def staged(optional):
    recorder = Recorder()
    data = {"brief": BRIEF, "title": "메모"}
    if optional is not None:
        data["optional_groups"] = optional
    payload = staged_specs(data, lambda kind, value: recorder(kind, value))
    payload["documents"]["개발기획서"] = "# 원본 개발기획서\n\n" + BRIEF
    return recorder, payload


def test_catalog_has_thirteen_required_and_nine_optional_documents_in_22_places():
    assert len(DOCUMENTS) == 22 and len(REQUIRED_DOCUMENTS) == 13 and len(OPTIONAL_DOCUMENTS) == 9
    assert (len(DETAIL), len(MANAGEMENT)) == (6, 3)
    assert "개발기획서" in REQUIRED_DOCUMENTS and "요구사항추적표" in REQUIRED_DOCUMENTS


def test_required_only_writes_two_groups_and_keeps_a_place_for_the_rest():
    recorder, payload = staged([])
    assert [kind for kind, _ in recorder.calls] == ["spec_requirements"] + ["spec_documents"] * len(REQUIRED_GROUPS)
    for _, data in recorder.calls[1:]:
        assert not set(data["other_documents"]) & set(OPTIONAL_DOCUMENTS)
        assert data["not_written_documents"] == list(OPTIONAL_DOCUMENTS)
    assert payload["skipped_documents"] == list(OPTIONAL_DOCUMENTS)
    assert payload["documents"]["용어사전"] == skipped_document("용어사전")
    assert set(validate_specs(payload, BRIEF)["documents"]) == set(DOCUMENTS)
    assert payload["skipped_documents"] == list(OPTIONAL_DOCUMENTS)


def test_one_optional_group_is_written_and_the_other_skipped():
    recorder, payload = staged(["detail"])
    written = [tuple(data["documents_to_write"]) for kind, data in recorder.calls[1:]]
    assert written == [names for _, names in REQUIRED_GROUPS] + [DETAIL]
    assert payload["skipped_documents"] == list(MANAGEMENT)
    assert "작성하지 않았습니다" not in payload["documents"]["클래스설계서"]


def test_no_selection_writes_everything_as_before():
    recorder, payload = staged(None)
    assert len(recorder.calls) == 1 + len(REQUIRED_GROUPS) + len(OPTIONAL_GROUPS)
    assert payload["skipped_documents"] == []
    assert all("not_written_documents" not in data for _, data in recorder.calls[1:])


@pytest.mark.parametrize("optional", [["unknown"], "detail", ["detail", "detail"]])
def test_an_unknown_selection_is_refused(studio, optional):  # noqa: F811
    data = studio.create("메모", BRIEF)
    jobs = StudioJobs(studio, fixture_generator)
    with pytest.raises(StudioError, match="선택 문서 묶음"):
        jobs.start(data["project"]["id"], data["project"]["version"], "specs", optional=optional)
    jobs.close()


def test_a_skipped_document_written_by_hand_is_no_longer_skipped():
    _, payload = staged([])
    payload["documents"]["용어사전"] = "# 용어사전\n\n| 용어 | 뜻 |\n|---|---|\n| 메모 | 사용자가 제목과 내용을 입력해 저장한 기록 |"
    validate_specs(payload, BRIEF)
    assert "용어사전" not in payload["skipped_documents"] and len(payload["skipped_documents"]) == 8
    payload["skipped_documents"].append("아키텍처설계서")  # a required document can never be skipped
    with pytest.raises(StudioError, match="작성하지 않은 문서"):
        validate_specs(payload, BRIEF)


def test_regenerating_a_skipped_document_writes_it_from_scratch():
    _, payload = staged([])
    seen = {}

    def call(kind, data):
        seen.update(data)
        return {"documents": {"용어사전": "# 용어사전\n\n" + "메모와 검색 결과를 설명하는 용어 정의입니다. " * 3}}

    result = regenerate_documents({"brief": BRIEF, "title": "메모"}, payload, ["용어사전"], "", call)
    assert "current_documents" not in seen or "용어사전" not in seen["current_documents"]
    assert "용어사전" not in seen["not_written_documents"] and "산출물목록" in seen["not_written_documents"]
    assert result["skipped_documents"] == [n for n in OPTIONAL_DOCUMENTS if n != "용어사전"]
    assert "용어 정의" in result["documents"]["용어사전"]


def test_document_prompt_explains_the_standard_contents():
    data = document_input({"brief": BRIEF}, {"requirements": []}, ["유스케이스명세서", "시험시나리오", "엔티티관계모형기술서"])
    prompt = prompt_for("spec_documents", data)
    assert "basic flow" in prompt and "단위시험" in prompt and "인수시험" in prompt
    assert '"엔티티관계모형기술서":"erDiagram"' in prompt


def test_review_reads_only_written_documents():
    _, payload = staged([])
    validate_specs(payload, BRIEF)
    data = spec_review.review_input(payload, None, "design", "Korean")
    assert set(data["specification"]["documents"]) == set(REQUIRED_DOCUMENTS)
    assert spec_review.anchors(payload)["document"] == set(REQUIRED_DOCUMENTS)


def old_names(payload):
    back = {new: old for old, new in RENAMED.items()}
    return dict(payload, documents={back.get(n, n): c for n, c in payload["documents"].items()})


def test_a_spec_saved_under_earlier_names_is_read_approved_and_exported(studio):  # noqa: F811
    data = studio.create("메모", BRIEF)
    project = data["project"]["id"]
    saved = old_names(specifications())
    saved["failed_documents"] = ["테스트시나리오"]
    with studio.store.transaction() as db:
        studio.store.insert_revision(db, "specs", project, saved, BRIEF)
        studio.store.bump(db, project, "spec.generated", {})
        stored = db.execute("SELECT digest FROM specs WHERE project_id=?", (project,)).fetchone()[0]
    data = studio.store.detail(project)
    assert set(data["spec"]["payload"]["documents"]) == set(DOCUMENTS)
    assert data["spec"]["payload"]["failed_documents"] == ["시험시나리오"]
    assert data["spec"]["digest"] == stored  # the saved row is untouched
    data = studio.approve_specs(project, data["project"]["version"])
    with studio.store.transaction() as db:
        studio.store.insert_revision(db, "mocks", project, mockup(), 1)
        studio.store.bump(db, project, "mock.generated", {})
    data = studio.store.detail(project)
    for screen in ("SCR-list", "SCR-form"):
        data = studio.approve_screen(project, data["project"]["version"], 1, screen)
    with ZipFile(io.BytesIO(studio.bundle(project, True))) as zipped:
        assert "documents/요구사항추적표.md" in zipped.namelist()
        assert "documents/요구사항추적성매트릭스.md" not in zipped.namelist()
        assert '"spec_digest":"' + stored + '"' in zipped.read("decisions.json").decode().replace(" ", "")


def test_current_names_leaves_unknown_shapes_alone():
    both = {"documents": {"테스트시나리오": "a", "시험시나리오": "b"}}
    assert current_names(both) is both
    assert current_names({"documents": None}) == {"documents": None}
    assert current_names("x") == "x"


def test_handoff_leaves_out_unwritten_documents_and_names_the_renames(studio):  # noqa: F811
    data = studio.create("메모", BRIEF)
    project = data["project"]["id"]
    jobs = StudioJobs(studio, fixture_generator)
    jobs.start(project, data["project"]["version"], "specs", optional=["detail"])
    data = wait_done(studio, project)
    jobs.close()
    assert data["spec"]["payload"]["skipped_documents"] == list(MANAGEMENT)
    data = studio.approve_specs(project, data["project"]["version"])
    with studio.store.transaction() as db:
        studio.store.insert_revision(db, "mocks", project, mockup(), data["spec"]["revision"])
        studio.store.bump(db, project, "mock.generated", {})
    data = studio.store.detail(project)
    for screen in ("SCR-list", "SCR-form"):
        data = studio.approve_screen(project, data["project"]["version"], data["mock"]["revision"], screen)
    with ZipFile(io.BytesIO(studio.bundle(project, True))) as zipped:
        assert len([n for n in zipped.namelist() if n.startswith("documents/")]) == 22
        handoff = zipped.read("development-handoff.md").decode()
    assert "\n## 클래스설계서\n" in handoff and "\n## 용어사전\n" not in handoff
    assert "화면정의서→사용자인터페이스설계서" in handoff
