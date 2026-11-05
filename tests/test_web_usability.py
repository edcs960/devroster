import http.client
import json
import shlex
import threading

import pytest
from test_reusable_workspace import Runner, approved

from test_reusable_workspace import workspace as shared_workspace

from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.dashboard import make_server
from agent_dev.ui.presentation import phase_label, task_display




@pytest.fixture(name="workspace")
def portable(tmp_path):
    return shared_workspace.__wrapped__(tmp_path)


@pytest.fixture
def legacy(tmp_path):
    (tmp_path / "data").mkdir()
    tasks = [
        {
            "id": "OLD-1",
            "title": "기존 작업",
            "status": "done",
            "scope": ["조회"],
            "acceptance_criteria": ["원본 보존"],
        },
        {"id": "OLD-2", "title": "검증 대기", "status": "review_ready", "depends_on": ["OLD-1"]},
        {"id": "OLD-3", "title": "진행 중", "status": "in_progress"},
        {
            "id": "OLD-4",
            "title": "실패",
            "status": "verification_failed",
            "depends_on": ["missing"],
        },
    ]
    (tmp_path / "data/backlog.json").write_text(json.dumps({"tasks": tasks}))
    return tmp_path


def test_legacy_detail_and_alias_filters_preserve_original_files(legacy):
    path = legacy / "data/backlog.json"
    before = path.read_bytes()
    query = TaskQueries(legacy)
    assert [row["id"] for row in query.tasks(state="completed")["tasks"]] == ["OLD-1"]
    assert query.tasks(state="verifying")["tasks"][0]["legacy_state"] == "review_ready"
    assert query.tasks()["counts"] == {"completed": 1, "verifying": 1, "running": 1, "blocked": 1}
    detail = query.explain("OLD-1")
    assert detail["task"]["definition"]["acceptance_criteria"] == ["원본 보존"]
    assert detail["approvals"] == {"design": None, "plan": None, "snapshot": None}
    assert query.explain("OLD-2")["dependencies"][0]["state"] == "completed"
    assert "missing" in query.explain("OLD-4")["reasons"][1]["message"]
    assert task_display(detail)["progress_percent"] is None
    assert shlex.split(task_display(detail)["actions"][-1]["command"]) == ["devroster", "resume", "OLD-1"]
    assert path.read_bytes() == before
    assert not (legacy / ".agent-dev").exists()
    with pytest.raises(DomainError):
        query.explain("unknown")


def test_current_failure_is_explained_and_stale_failure_is_not(workspace):
    runtime = approved(workspace)

    class Fail:
        def execute(self, request):
            raise DomainError(
                ErrorCode.INVALID_INPUT, "Harness output does not satisfy its contract."
            )

    runtime.execute("T", "fail", runner=Fail())
    detail = TaskQueries(workspace).explain("T")
    failure = next(row for row in detail["reasons"] if row["code"] == "INVALID_INPUT")
    assert "Harness" in failure["message"]
    assert task_display(detail)["state_label"] == "문제 해결이 필요해요"
    with runtime.store.db.transaction() as sql:
        sql.execute("UPDATE tasks SET state='running' WHERE id='T'")
    with runtime.safety.lease("task:T") as lease:
        runtime.store.begin_attempt("T", "planner", "new-attempt", lease)
    with runtime.store.db.transaction() as sql:
        sql.execute("UPDATE tasks SET state='blocked' WHERE id='T'")
    assert "INVALID_INPUT" not in {
        row["code"] for row in TaskQueries(workspace).explain("T")["reasons"]
    }


@pytest.mark.parametrize(
    "state", ["pr_ready", "user_review", "feedback_required", "completed", "cancelled"]
)
def test_next_action_never_restarts_review_feedback_or_terminal_tasks(workspace, state):
    runtime = approved(workspace)
    with runtime.store.db.transaction() as sql:
        sql.execute("UPDATE tasks SET state=? WHERE id='T'", (state,))
    detail = TaskQueries(workspace).explain("T")
    assert not any("task run" in action for action in detail["next_actions"])
    view = task_display(detail)
    assert all("task run" not in action["command"] for action in view["actions"])
    if state == "feedback_required":
        assert any(row["code"] == "FEEDBACK_PENDING" for row in detail["reasons"])


def test_progress_and_guidance_use_real_attempts_and_safe_commands(workspace):
    runtime = approved(workspace)
    ready = task_display(TaskQueries(workspace).explain("T"))
    assert ready["progress_percent"] == 0
    assert ready["actions"][-1]["command"] == "devroster task run T"
    runtime.execute("T", "run", runner=Runner())
    view = task_display(TaskQueries(workspace).explain("T"))
    assert view["completed_phases"] == view["total_phases"] == 3
    assert view["actions"][-1]["command"] == "devroster task verify T"
    assert phase_label("08-developer-frontend") == "화면 개발"


def serve(service):
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    return thread


def call(service, method, path, body=None, headers=None):
    client = http.client.HTTPConnection(*service.server_address, timeout=5)
    client.request(method, path, body=body, headers=headers or {})
    response = client.getresponse()
    result = response.status, response.read()
    client.close()
    return result


def test_dashboard_legacy_detail_safe_boundary_and_portable_assets(legacy):
    service = make_server(legacy, 0)
    thread = serve(service)
    try:
        code, body = call(service, "GET", "/api/task?id=OLD-2")
        assert code == 200 and json.loads(body)["display"]["source_label"] == "기존 작업 기록"
        assert call(service, "GET", "/api/task?id=missing")[0] == 400
        assert (
            call(service, "GET", "/api/task?id=OLD-2", headers={"Origin": "https://bad.example"})[0]
            == 403
        )
        assert call(service, "POST", "/api/task?id=OLD-2")[0] == 405
        assert b"dashboard.js" in call(service, "GET", "/")[1]
        assert call(service, "GET", "/dashboard.js")[0] == 200
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=2)




def test_skipped_roles_are_not_counted_as_waiting():
    """Roles skipped by their condition showed as 대기 and as 8/14."""
    detail = {
        "source": "canonical",
        "task": {"task_id": "T", "state": "verifying"},
        "reasons": [],
        "attempts": [{"phase_id": "planner", "state": "passed"}],
        "node_states": {"planner": "passed", "architect": "passed", "dba": "skipped"},
        "snapshot": {"phases": ["planner", "architect", "dba"]},
    }
    view = task_display(detail)
    assert [p["state"] for p in view["phases"]] == ["passed", "passed", "skipped"]
    assert (view["completed_phases"], view["total_phases"]) == (2, 2)
