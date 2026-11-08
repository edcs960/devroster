"""The project's working language: every AI-written text, commit and pull request follows it."""

import json
from pathlib import Path

import pytest
from test_reusable_workspace import approved, workspace  # noqa: F401 - fixture

from agent_dev.adapters.harness import parse_harness
from agent_dev.adapters.publishing import PublishAdapter
from agent_dev.domain import output_language
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError


def test_language_values_are_bounded_single_lines():
    assert output_language.normalize(None) == "" and output_language.normalize(" English ") == "English"
    for bad in ["x" * 41, "한국어\n영어", 3]:
        with pytest.raises(DomainError):
            output_language.normalize(bad)
    assert output_language.language({}) == "한국어"
    assert output_language.language({"output_language": "日本語"}) == "日本語"


def test_commit_messages_are_cleaned_or_refused():
    clean = output_language.clean_commit_message
    assert clean("feat: 회원 가입 화면 추가\r\n\r\n이유와 검증") == "feat: 회원 가입 화면 추가\n\n이유와 검증"
    for bad in [None, "", "  ", "x" * 101, "fix: " + "y" * 2000, "fix: token ghp_" + "a" * 30]:
        assert clean(bad) is None


def test_code_roles_may_report_a_commit_message():
    decision = parse_harness('```json\n{"handoff_status":"pass","commit_message":"feat: 추가"}\n```')
    assert decision["commit_message"] == "feat: 추가"


def set_language(root, value):
    path = root / ".agent-dev/config/profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["output_language"] = value
    path.write_text(json.dumps(profile), encoding="utf-8")


def test_prompts_carry_the_language_and_the_commit_rule(workspace):  # noqa: F811
    runtime = approved(workspace)
    context = json.loads(runtime.artifacts.context("T", "developer"))
    assert "in 한국어" in context["output_language_rule"]  # Default.
    assert '"commit_message"' in context["commit_message_rule"] and "in 한국어" in context["commit_message_rule"]


class CommitRunner:
    def __init__(self, message):
        self.message = message

    def execute(self, request):
        if request.phase_id == "developer":
            binding = json.loads(request.context)["repositories"]["source"]
            (Path(binding["worktree"]) / "src/source.txt").write_text("implemented", encoding="utf-8")
            control = {"handoff_status": "pass", "commit_message": self.message}
        else:
            control = {"handoff_status": "pass"}
        return PhaseResult("```json\n" + json.dumps(control, ensure_ascii=False) + "\n```")


def test_publish_uses_the_ai_commit_message_and_writes_the_pr_from_it(workspace):  # noqa: F811
    runtime = approved(workspace)
    runtime.execute("T", "run", runner=CommitRunner("feat: 소스 구현\n\n요청한 동작을 구현했다. 단위 검증으로 확인."))
    runtime.execute("T", "verify", verify=True)
    adapter = PublishAdapter(runtime)
    message = adapter.commit_message("T", "source")
    assert message == "feat: 소스 구현\n\n요청한 동작을 구현했다. 단위 검증으로 확인.\n\nDevRoster-Task: T"
    body = adapter.pull_request_body("T", message)
    assert body.startswith("요청한 동작을 구현했다.") and "## 검증" in body and "통과" in body


def test_unsafe_or_missing_commit_message_falls_back_to_the_task_title(workspace):  # noqa: F811
    runtime = approved(workspace)
    runtime.execute("T", "run", runner=CommitRunner("feat: key sk-" + "a" * 30))
    assert PublishAdapter(runtime).commit_message("T", "source") == "Example task\n\nDevRoster-Task: T"


def test_pull_request_headings_follow_a_non_korean_language(workspace):  # noqa: F811
    runtime = approved(workspace)
    adapter = PublishAdapter(runtime)
    snapshot = runtime.store.snapshot

    def english(task_id):
        value = snapshot(task_id)
        value["resolved"]["output_language"] = "English"
        return value

    runtime.store.snapshot = english
    assert "## Verification" in adapter.pull_request_body("T", "feat: add\n\nWhy")


def test_setup_saves_and_reads_the_language(tmp_path):
    from test_project_setup import commit, git

    from agent_dev.adapters.project_setup import ProjectSetup

    project = tmp_path / "proj"
    project.mkdir()
    git(project, "init", "-q")
    (project / "f").write_text("x", encoding="utf-8")
    commit(project)
    setup = ProjectSetup(project)
    value = setup.read()
    value["output_language"] = "English"
    setup.save(value)
    assert ProjectSetup(project).read()["output_language"] == "English"
    assert setup.current_profile()[2]["output_language"] == "English"
    value = ProjectSetup(project).read()
    value["output_language"] = "x" * 41
    with pytest.raises(DomainError):
        ProjectSetup(project).save(value)


def test_studio_prompt_follows_the_language():
    from agent_dev.adapters.studio_generation import prompt_for

    assert "written in 한국어" in prompt_for("specs", {"brief": "기획서"})
    english = prompt_for("specs", {"brief": "brief", "project_guidance": {"language": "English"}})
    assert "written in English" in english and "PROJECT RULES" not in english
