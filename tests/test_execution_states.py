"""Execution view over real runtime states: skip, approval, capacity, lease, cancel."""


from test_workflow_dag import Passing, branch, prepare

from agent_dev.adapters import execution_view
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext

pytest_plugins = ["test_workflow_runtime"]


def test_skipped_branch_and_awaiting_approval_are_counted(runtime):
    prepare(runtime, branch(), {"workstreams": ["frontend"]})
    runtime.execute("DAG", "first", runner=Passing())
    data = execution_view.view(runtime.root, "DAG")
    items = {row["id"]: row["state"] for row in data["stage_items"]}
    assert items["backend"] == "skipped" and items["human"] == "awaiting_approval"
    assert data["stages"]["skipped"] == 1 and data["stages"]["awaiting_approval"] == 1
    assert data["stages"]["total"] == 5  # ui, backend, human, developer, verify
    assert "choose" not in items and "join" not in items
    assert data["terminal"] is False


class Exhausted:
    def execute(self, request):
        raise DomainError(ErrorCode.CAPACITY_EXHAUSTED, "quota")


def test_capacity_wait_lease_and_cancelled_terminal(runtime):
    prepare(runtime, branch(), {"workstreams": ["backend"]})
    runtime.execute("DAG", "first", runner=Exhausted())
    data = execution_view.view(runtime.root, "DAG")
    assert data["task"]["state"] == "waiting_for_capacity" and data["wait"]["capacity"] is True
    assert data["task"]["state_label"] == "AI 사용량 대기"
    assert any(row["code"] == "CAPACITY_WAIT" and "한도" in row["message"] for row in data["next_actions"])
    assert any(event["kind"] == "ExecutionBlocked" for event in data["timeline"])
    with runtime.safety.lease("task:DAG") as lease:
        held = execution_view.view(runtime.root, "DAG", cursor=data["cursor"])
        assert held["wait"]["lease_expires_at"] and held["wait"]["note"]
        task = runtime.store.task("DAG")
        runtime.store.transition("DAG", TaskStatus.CANCELLED, TransitionContext(expected_version=task.version), lease)
    done = execution_view.view(runtime.root, "DAG", cursor=data["cursor"])
    assert done["terminal"] is True and done["poll"] == {"active_ms": 0, "hidden_ms": 0}
    assert done["wait"]["lease_expires_at"] is None
    assert [e["kind"] for e in done["timeline"]] and done["cursor"] > data["cursor"]
    assert execution_view.view(runtime.root, "DAG", cursor=done["cursor"])["timeline"] == []
