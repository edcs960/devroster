"""A crashed attempt's leftovers are checked against its role before the step runs again."""

import json
from pathlib import Path

import pytest
from test_budget_pause import runtime_with

from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.domain.contracts import PhaseResult

PASS = '```json\n{"handoff_status":"pass","summary":"done"}\n```'
POLICY = {"estimated_budget": 100.0, "price_book": []}
PHASES = [{"id": "developer", "capabilities": ["code.write"]}, {"id": "reviewer", "capabilities": ["code.read"]}]
GRAPH = {
    "schema_version": 1,
    "id": "dev-review",
    "name": "Develop and review",
    "nodes": [
        {"id": "developer", "kind": "role", "role_id": "developer"},
        {"id": "reviewer", "kind": "role", "role_id": "reviewer"},
        {"id": "verify", "kind": "verification", "gate_ids": ["unit"]},
    ],
    "edges": [{"from": "developer", "to": "reviewer"}, {"from": "reviewer", "to": "verify"}],
}


class Crash(BaseException):
    """The process dies mid-step: nothing after this runs, the attempt stays 'running'."""


class Runner:
    def __init__(self, crash_in, write):
        self.crash_in, self.write, self.calls = crash_in, write, []

    def execute(self, request):
        self.calls.append(request.phase_id)
        root = Path(json.loads(request.context)["repositories"]["source"]["worktree"])
        if request.phase_id == "developer" and self.calls.count("developer") == 1:
            (root / "source.txt").write_text("developer work", encoding="utf-8")
        if request.phase_id == self.crash_in and self.calls.count(self.crash_in) == 1:
            (root / "source.txt").write_text(self.write, encoding="utf-8")
            raise Crash()
        return PhaseResult(PASS, provider="codex_cli")


def worktree(runtime):
    return Path(runtime.store.snapshot("T")["resolved"]["repositories"]["source"]["worktree"])


def kinds(runtime):
    return [r["kind"] for r in runtime.store.db.rows("SELECT kind FROM events WHERE aggregate_id='T' ORDER BY version")]


@pytest.mark.parametrize("graph", [None, GRAPH], ids=["phases", "workflow-graph"])
def test_read_only_role_leftovers_stop_the_task_and_resume_undoes_them(tmp_path, graph):
    runtime = runtime_with(tmp_path, POLICY, graph, PHASES)
    runner = Runner("reviewer", "reviewer edited this")
    with pytest.raises(Crash):
        runtime.execute("T", "run-1", runner=runner)
    stopped = runtime.execute("T", "run-2", runner=runner)
    assert stopped["state"] == "blocked"
    assert "INTERRUPTED_CHANGES" in [r["code"] for r in TaskQueries(runtime.root).explain("T")["reasons"]]
    folder = worktree(runtime)
    assert (folder / "source.txt").read_text(encoding="utf-8") == "reviewer edited this"
    copies = list((folder.parent / ".attempts").glob("*-interrupted/source/source.txt"))
    assert [c.read_text(encoding="utf-8") for c in copies] == ["reviewer edited this"]
    resumed = runtime.execute("T", "run-3", runner=runner)
    assert resumed["state"] != "blocked"
    # Back to where the reviewer started: the developer's (uncommitted) work is kept.
    assert (folder / "source.txt").read_text(encoding="utf-8") == "developer work"
    assert "InterruptedChangesDiscarded" in kinds(runtime)


@pytest.mark.parametrize("graph", [None, GRAPH], ids=["phases", "workflow-graph"])
def test_code_role_partial_work_is_kept_and_continued(tmp_path, graph):
    runtime = runtime_with(tmp_path, POLICY, graph, PHASES)
    runner = Runner("developer", "half done")
    with pytest.raises(Crash):
        runtime.execute("T", "run-1", runner=runner)
    result = runtime.execute("T", "run-2", runner=runner)
    assert result["state"] != "blocked"
    assert "InterruptedChangesKept" in kinds(runtime)
    assert (worktree(runtime) / "source.txt").read_text(encoding="utf-8") == "half done"
