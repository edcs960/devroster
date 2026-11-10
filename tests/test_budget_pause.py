"""Opt-in pause at the estimated budget: stop before the next AI call, resume approves one more budget."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError
from agent_dev.domain.usage_policy import budget_pause_due, normalize_policy

PRICE = [{"provider": "codex_cli", "model": "gpt-x", "input_per_million": 1.0, "output_per_million": 1.0}]
# Each call: 600k input + 400k output tokens = 1.0 USD at the price above.
USAGE = {"input_tokens": 600_000, "output_tokens": 400_000}


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class Runner:
    def __init__(self):
        self.calls = []

    def execute(self, request):
        self.calls.append(request.phase_id)
        root = Path(json.loads(request.context)["repositories"]["source"]["worktree"])
        (root / "source.txt").write_text(f"step {len(self.calls)}", encoding="utf-8")
        return PhaseResult(
            '```json\n{"handoff_status":"pass","summary":"done"}\n```',
            provider="codex_cli",
            usage=USAGE,
            selection={"model": "gpt-x", "effort": "medium", "reason": "test"},
        )


GRAPH = {
    "schema_version": 1,
    "id": "three-steps",
    "name": "Three steps",
    "nodes": [
        {"id": "d1", "kind": "role", "role_id": "developer"},
        {"id": "d2", "kind": "role", "role_id": "developer"},
        {"id": "d3", "kind": "role", "role_id": "developer"},
        {"id": "verify", "kind": "verification", "gate_ids": ["unit"]},
    ],
    "edges": [{"from": "d1", "to": "d2"}, {"from": "d2", "to": "d3"}, {"from": "d3", "to": "verify"}],
}


def runtime_with(tmp_path, policy, graph=None, phases=None, gate=None):
    source = tmp_path / "source"
    source.mkdir()
    (source / "source.txt").write_text("base", encoding="utf-8")
    git(source, "init", "-q")
    git(source, "add", "-A")
    git(source, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "base")
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(json.dumps({"orchestrator_v2": {"execute": True}}), encoding="utf-8")
    runtime = WorkflowRuntime(root)
    runtime.store.register_project(
        "P",
        "Project",
        {
            "repositories": {"source": {"path": str(source), "allowed_paths": ["source.txt"]}},
            "phases": phases
            or [
                {"id": "developer", "capabilities": ["code.write"]},
                {"id": "developer2", "capabilities": ["code.write"]},
                {"id": "developer3", "capabilities": ["code.write"]},
            ],
            "verification": {"commands": [{"name": "unit", "repository": "source", "command": gate or [sys.executable, "-c", "print(1)"]}], "required_gates": ["unit"]},
            "usage_policy": policy,
            **({"workflow": {"document": graph}} if graph else {}),
        },
    )
    runtime.store.design("P", "D", "## Scope\nS\n## Acceptance\nA\n## Constraints\nC")
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan("P", "PLAN", "D", 1, [{"id": "T", "title": "Execution task"}])
    runtime.store.approve("plan", "PLAN", 1, "user")
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    return runtime


def test_pause_needs_a_budget_and_is_off_by_default():
    assert normalize_policy({"estimated_budget": 1.0, "price_book": PRICE})["pause_at_budget"] is False
    with pytest.raises(DomainError):
        normalize_policy({"pause_at_budget": True, "price_book": PRICE})
    with pytest.raises(DomainError):
        normalize_policy({"pause_at_budget": "yes", "estimated_budget": 1.0})


def test_each_resume_allows_one_more_budget():
    policy = normalize_policy({"estimated_budget": 2.0, "pause_at_budget": True, "price_book": PRICE})
    assert not budget_pause_due(policy, {"known_cost": 1.9})
    assert budget_pause_due(policy, {"known_cost": 2.0})
    assert not budget_pause_due(policy, {"known_cost": 3.5}, approved_cost=2.0)
    assert budget_pause_due(policy, {"known_cost": 4.0}, approved_cost=2.0)
    off = normalize_policy({"estimated_budget": 2.0, "price_book": PRICE})
    assert not budget_pause_due(off, {"known_cost": 100})


@pytest.mark.parametrize("graph", [None, GRAPH], ids=["phases", "workflow-graph"])
def test_task_pauses_at_budget_and_resume_continues(tmp_path, graph):
    runtime = runtime_with(tmp_path, {"estimated_budget": 2.0, "pause_at_budget": True, "price_book": PRICE}, graph)
    runner = Runner()
    first = runtime.execute("T", "run-1", runner=runner)
    # Two calls cost 2.0 USD: the third phase waits for the user.
    assert first["state"] == "blocked" and len(runner.calls) == 2
    explained = TaskQueries(runtime.root).explain("T")
    assert "BUDGET_PAUSE" in [row["code"] for row in explained["reasons"]]
    resumed = runtime.execute("T", "run-2", runner=runner)
    assert resumed["state"] != "blocked" and len(runner.calls) == 3
    approvals = runtime.store.db.rows("SELECT payload_json FROM events WHERE aggregate_id='T' AND kind='BudgetPauseApproved'")
    assert [json.loads(r["payload_json"])["known_cost"] for r in approvals] == [2.0]
    kinds = {r["kind"] for r in runtime.store.db.rows("SELECT kind FROM events WHERE aggregate_id='T'")}
    assert ("WorkflowRoleResult" in kinds) is bool(graph)  # Both coordinators are covered.


def test_without_pause_the_budget_only_alerts(tmp_path):
    runtime = runtime_with(tmp_path, {"estimated_budget": 1.0, "alert_thresholds": [1.0], "price_book": PRICE})
    runner = Runner()
    result = runtime.execute("T", "run-1", runner=runner)
    assert result["state"] != "blocked" and len(runner.calls) == 3
