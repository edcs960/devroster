"""A missing or malformed result block gets one format-only repair in the same conversation."""

import json
from pathlib import Path

import pytest
from test_budget_pause import GRAPH, runtime_with

from agent_dev.adapters.harness import parse_harness
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError

PASS = '```json\n{"handoff_status":"pass","summary":"done"}\n```'
POLICY = {"estimated_budget": 100.0, "price_book": []}


class Runner:
    """First answer per phase is malformed; a repair request gets `repair_output`."""

    def __init__(self, first, repair_output=PASS, session="codex_cli:S1", touch_on_repair=False):
        self.first, self.repair_output, self.session = first, repair_output, session
        self.touch_on_repair, self.requests = touch_on_repair, []

    def execute(self, request):
        self.requests.append(request)
        context = json.loads(request.context)
        root = Path(context["repositories"]["source"]["worktree"])
        if "control_repair" in context:
            if self.touch_on_repair:
                (root / "source.txt").write_text("changed during repair", encoding="utf-8")
            return PhaseResult(self.repair_output, provider="codex_cli", usage={"output_tokens": 5}, resume_session=self.session)
        (root / "source.txt").write_text(f"work {len(self.requests)}", encoding="utf-8")
        output = self.first if len(self.requests) == 1 else PASS
        return PhaseResult(output, provider="codex_cli", usage={"output_tokens": 50}, resume_session=self.session)


def events(runtime, kind):
    return runtime.store.db.rows("SELECT payload_json FROM events WHERE aggregate_id='T' AND kind=?", (kind,))


def test_unfenced_block_at_the_end_is_accepted_but_not_an_earlier_example():
    assert parse_harness('Done.\n{"handoff_status": "pass", "summary": "ok"}\n')["handoff_status"] == "pass"
    with pytest.raises(DomainError):
        parse_harness('Example: {"handoff_status": "pass"} and then more text.')


@pytest.mark.parametrize("graph", [None, GRAPH], ids=["phases", "workflow-graph"])
@pytest.mark.parametrize("first", ["work done, no block", '```json\n{"handoff_status": "pass",}\n```'])
def test_format_error_is_repaired_once_in_the_same_conversation(tmp_path, first, graph):
    runtime = runtime_with(tmp_path, POLICY, graph)
    runner = Runner(first)
    result = runtime.execute("T", "run-1", runner=runner)
    assert result["state"] != "blocked"
    repair = runner.requests[1]
    assert repair.resume_session == "codex_cli:S1" and repair.phase_id == runner.requests[0].phase_id
    assert len(events(runtime, "ControlRepairRequested")) == 1


@pytest.mark.parametrize("graph", [None, GRAPH], ids=["phases", "workflow-graph"])
def test_repair_that_changes_files_is_refused(tmp_path, graph):
    runtime = runtime_with(tmp_path, POLICY, graph)
    result = runtime.execute("T", "run-1", runner=Runner("no block", touch_on_repair=True))
    assert result["state"] == "blocked"
    assert events(runtime, "ControlRepairRequested") == []


def test_no_repair_without_a_resumable_session_or_for_a_real_answer(tmp_path):
    runtime = runtime_with(tmp_path, POLICY)
    runner = Runner("no block", session="")
    assert runtime.execute("T", "run-1", runner=runner)["state"] == "blocked"
    assert len(runner.requests) == 1
    (tmp_path / "b").mkdir()
    other = runtime_with(tmp_path / "b", POLICY)
    unknown_field = '```json\n{"handoff_status":"pass","extra":"x"}\n```'
    runner = Runner(unknown_field)
    assert other.execute("T", "run-1", runner=runner)["state"] == "blocked"
    assert len(runner.requests) == 1


def test_still_broken_after_repair_blocks(tmp_path):
    runtime = runtime_with(tmp_path, POLICY)
    runner = Runner("no block", repair_output="still no block")
    assert runtime.execute("T", "run-1", runner=runner)["state"] == "blocked"
    assert len(runner.requests) == 2
