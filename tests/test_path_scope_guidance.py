"""A refused file change says which file, what the approved scope is and how to resolve it."""

import json
from pathlib import Path

import pytest
from test_budget_pause import runtime_with

from agent_dev.adapters import execution_view
from agent_dev.adapters.role_execution import capture, enforce
from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.role_policy import (
    allowed_path,
    apply_structured,
    listed,
    path_refusal,
    policy,
    scope_text,
    session_policy,
    validate_changes,
)

PASS = '```json\n{"handoff_status":"pass","summary":"done"}\n```'
POLICY = {"estimated_budget": 100.0, "price_book": []}
PHASES = [{"id": "developer", "capabilities": ["code.write"]}]
DEVELOPER = policy("developer", project_paths=["src", "tests"])


def refused(tmp_path, paths, role_policy, **options):
    with pytest.raises(DomainError) as error:
        validate_changes(tmp_path, paths, role_policy, **options)
    assert error.value.code is ErrorCode.POLICY_VIOLATION
    return str(error.value)


def test_scope_message_names_the_file_the_scope_and_the_way_out(tmp_path):
    message = refused(tmp_path, ["src/app.py", "package.json"], DEVELOPER)
    assert "수정 허용 경로 밖의 파일을 바꿨습니다: package.json." in message
    assert "src/app.py" not in message  # An allowed change is not listed.
    assert "이 단계의 수정 허용 경로: src, tests." in message
    assert "'수정 허용 경로'" in message and "계획을 새로 승인" in message
    assert "되돌린 뒤 이어서 실행" in message


def test_changes_inside_the_scope_pass(tmp_path):
    validate_changes(tmp_path, ["src/app.py", "tests/test_app.py"], DEVELOPER)


@pytest.mark.parametrize(
    ("path", "role_policy", "reason"),
    [
        ("src/app.py", DEVELOPER, None),
        ("package.json", DEVELOPER, "scope"),
        ("src/app.py", policy("reviewer", project_paths=["src"]), "readonly"),
        ("src/guide.md", policy("tech-writer", project_paths=["src", "docs"], document_paths=["docs"]), "documents"),
        ("docs/guide.md", policy("tech-writer", project_paths=["src", "docs"], document_paths=["docs"]), None),
        ("output/a.md", policy("tech-writer", project_paths=["src", "docs"], document_paths=["docs"]), "scope"),
        (".git/config", DEVELOPER, "unsafe"),
        ("src/../../outside.txt", DEVELOPER, "unsafe"),
        ("/etc/hosts", DEVELOPER, "unsafe"),
        ("src\\app.py", DEVELOPER, "unsafe"),
        ("", DEVELOPER, "unsafe"),
        (None, DEVELOPER, "unsafe"),
        # An unsafe path is refused for that reason even for a read-only role.
        (".git/config", policy("reviewer", project_paths=["src"]), "unsafe"),
    ],
)
def test_refusal_reason_and_permission_agree(tmp_path, path, role_policy, reason):
    assert path_refusal(tmp_path, path, role_policy) == reason
    assert allowed_path(tmp_path, path, role_policy) is (reason is None)


def test_each_reason_has_its_own_explanation(tmp_path):
    readonly = refused(tmp_path, ["src/app.py"], policy("reviewer", project_paths=["src"]))
    assert "파일을 직접 바꿀 수 없는 역할이 파일을 바꿨습니다: src/app.py." in readonly
    assert "수정 허용 경로" not in readonly  # Adding a path would not help a read-only role.
    assert readonly.endswith("작업 폴더에서 그 변경을 되돌린 뒤 이어서 실행하세요.")
    writer = policy("tech-writer", project_paths=["src", "docs"], document_paths=["docs"])
    assert "README 파일만 바꿀 수 있는데 다른 파일을 바꿨습니다: src/guide.md." in refused(
        tmp_path, ["src/guide.md"], writer
    )
    unsafe = refused(tmp_path, [".git/config"], DEVELOPER)
    assert "누구도 바꿀 수 없는 경로" in unsafe and ".git/config" in unsafe
    mixed = refused(tmp_path, ["package.json", ".git/config"], DEVELOPER)
    assert "밖의 파일을 바꿨습니다: package.json." in mixed and "바꿨습니다: .git/config." in mixed


def test_file_names_are_bounded_and_printable(tmp_path):
    many = refused(tmp_path, [f"file-{n}.txt" for n in range(9)], DEVELOPER)
    assert "file-0.txt, file-1.txt, file-2.txt, file-3.txt, file-4.txt 외 4개." in many
    assert "file-5.txt" not in many
    assert listed(["a\nb\x1b[31m.txt", "x" * 300, "bad\udc80.txt"]) == ", ".join(
        ["a?b?[31m.txt", "bad?.txt", "x" * 120]
    )
    listed(["bad\udc80.txt"]).encode("utf-8")  # A name Git decoded with surrogates stays printable.


def test_scope_text_shows_the_narrowest_groups():
    assert scope_text({"scopes": [["src", "tests"], ["src", "tests"]]}) == "src, tests"
    # The module's scope is inside the project's: only the narrower one decides.
    assert scope_text({"scopes": [["frontend/src", "backend/src"], ["frontend/src"]]}) == "frontend/src"
    assert scope_text({"scopes": [["src"], ["src/app"], ["src"]]}) == "src/app"
    # A file-name pattern cannot be compared with a folder, so both groups are shown.
    assert scope_text({"scopes": [["src", "tests"], ["tests", "*.test.*"]]}) == (
        "다음 범위에 모두 드는 경로: (src, tests) / (tests, *.test.*)"
    )
    assert scope_text({"scopes": [["src"], []]}) == "없음(허용 경로가 비어 있습니다)"
    union = session_policy(
        [policy("developer", project_paths=["a.txt"]), policy("developer-frontend", project_paths=["b.txt"])]
    )
    assert scope_text(union) == "a.txt 또는 b.txt"


def test_shown_scope_is_what_the_check_uses(tmp_path):
    """The narrowed text never names a scope the check would refuse, or hides one it accepts."""
    role_policy = dict(DEVELOPER, scopes=[["src", "tests"], ["src/app", "tests"], ["src", "tests"]])
    assert scope_text(role_policy) == "src/app, tests"
    assert allowed_path(".", "src/app/main.py", role_policy)
    assert allowed_path(".", "tests/test_main.py", role_policy)
    assert not allowed_path(".", "src/other.py", role_policy)


def test_structured_proposal_outside_scope_writes_nothing(tmp_path):
    tests_policy = policy("test-automation", project_paths=["src", "tests"], test_paths=["tests"])
    proposals = [
        {"path": "tests/test_ok.py", "content": "ok"},
        {"path": "src/app.py", "content": "not a test"},
    ]
    with pytest.raises(DomainError) as error:
        apply_structured(tmp_path, proposals, tests_policy)
    message = str(error.value)
    assert "수정 허용 경로 밖의 파일을 바꾸려 했습니다: src/app.py." in message
    assert "이 단계의 수정 허용 경로: tests." in message
    assert "파일에 적용된 것은 없습니다." in message and "되돌린" not in message
    assert not (tmp_path / "tests").exists()


def bindings(tmp_path, allowed):
    import subprocess

    result = {}
    for name, paths in allowed.items():
        root = tmp_path / name
        root.mkdir()
        (root / "keep.txt").write_text("base", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
        result[name] = {"worktree": str(root), "allowed_paths": paths}
    return result


def test_enforce_reports_the_module_scope(tmp_path):
    repositories = bindings(tmp_path, {"frontend": ["src"]})
    before = capture(repositories)
    root = Path(repositories["frontend"]["worktree"])
    (root / "src").mkdir()
    (root / "src/app.js").write_text("ok", encoding="utf-8")
    (root / "package.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        enforce(repositories, before, policy("developer", project_paths=["src", "tests"]), "frontend")
    message = str(error.value)
    assert "밖의 파일을 바꿨습니다: package.json." in message
    assert "이 단계의 수정 허용 경로: src." in message  # Narrowed by the module's own paths.


def test_enforce_names_the_other_module(tmp_path):
    repositories = bindings(tmp_path, {"frontend": ["src"], "backend": ["src"]})
    before = capture(repositories)
    root = Path(repositories["backend"]["worktree"])
    (root / "src").mkdir()
    (root / "src/api.py").write_text("ok", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        enforce(repositories, before, policy("developer", project_paths=["src"]), "frontend")
    assert error.value.code is ErrorCode.POLICY_VIOLATION
    message = str(error.value)
    assert "'frontend' 모듈만 바꿀 수 있는데 'backend' 모듈의 파일을 바꿨습니다: src/api.py." in message
    assert "계획을 새로 승인" in message


class OutOfScope:
    def execute(self, request):
        root = Path(json.loads(request.context)["repositories"]["source"]["worktree"])
        (root / "package.json").write_text("{}", encoding="utf-8")
        return PhaseResult(PASS, provider="codex_cli")


def test_blocked_task_shows_the_file_on_every_surface(tmp_path):
    runtime = runtime_with(tmp_path, POLICY, None, PHASES)
    result = runtime.execute("T", "run-1", runner=OutOfScope())
    assert result["state"] == "blocked" and result["error"] == "POLICY_VIOLATION"
    assert "package.json" in result["reason"] and "수정 허용 경로: source.txt." in result["reason"]
    reasons = TaskQueries(runtime.root).explain("T")["reasons"]
    row = next(r for r in reasons if r["code"] == "POLICY_VIOLATION")
    assert row["message"] == result["reason"]
    shown = next(r for r in execution_view.view(runtime.root, "T")["next_actions"] if r["code"] == "POLICY_VIOLATION")
    assert shown["message"] == result["reason"]
    assert shown["action"] == "위 안내를 확인한 뒤 이어서 실행하세요. (devroster task resume T)"


def test_publish_scope_error_names_the_files(tmp_path):
    from agent_dev.scm.git_publish import validate_publish_paths

    validate_publish_paths(tmp_path, ["src/app.py"], ["src"])
    with pytest.raises(DomainError) as error:
        validate_publish_paths(tmp_path, ["src/app.py", "package.json", "notes.txt"], ["src", "tests/"])
    assert error.value.code is ErrorCode.POLICY_VIOLATION
    message = str(error.value)
    assert "게시하지 않았습니다: notes.txt, package.json." in message
    assert "수정 허용 경로: src, tests." in message and "src/app.py" not in message


def test_other_unknown_codes_keep_their_action():
    rows = execution_view.next_actions(
        [{"code": "AUTH_REQUIRED", "message": "Runner requires authentication.", "action": "raw"}], "T"
    )
    assert rows == [{"code": "AUTH_REQUIRED", "message": "Runner requires authentication.", "action": "raw"}]
