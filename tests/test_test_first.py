"""Test-first preset: tests from the acceptance criteria must fail before development."""

import json
import sys
from pathlib import Path

from test_budget_pause import runtime_with

from agent_dev.adapters.workflow_definitions import PRESET_ROLES, preset
from agent_dev.domain.contracts import PhaseResult

POLICY = {"estimated_budget": 100.0, "price_book": []}
PASS = '```json\n{"handoff_status":"pass","summary":"ok"}\n```'
# The gate passes only once the developer has done the work.
GATE = [sys.executable, "-c", "import sys; sys.exit(0 if open('source.txt').read() == 'implemented' else 1)"]


class Runner:
    def __init__(self):
        self.calls = []

    def execute(self, request):
        self.calls.append(request.phase_id)
        if request.phase_id == "developer":
            root = Path(json.loads(request.context)["repositories"]["source"]["worktree"])
            (root / "source.txt").write_text("implemented", encoding="utf-8")
        return PhaseResult(PASS, provider="codex_cli")


def test_preset_puts_a_red_check_between_test_automation_and_development():
    graph = preset("test-first")
    assert PRESET_ROLES["test-first"] == ["planner", "test-automation", "developer", "qa", "reviewer"]
    red = next(n for n in graph["nodes"] if n["id"] == "red-check")
    assert red == {"id": "red-check", "kind": "verification", "gate_ids": [], "expect": "fail"}
    edges = {(e["from"], e["to"]) for e in graph["edges"]}
    assert ("test-automation", "red-check") in edges and ("red-check", "developer") in edges
    assert ("test-automation", "developer") not in edges


def test_failing_tests_before_development_let_the_work_continue(tmp_path):
    runtime = runtime_with(tmp_path, POLICY, preset("test-first"), gate=GATE)
    runner = Runner()
    assert runtime.execute("T", "run", runner=runner)["state"] == "verifying"
    assert runner.calls == ["planner", "test-automation", "developer", "qa", "reviewer"]


def test_tests_that_already_pass_go_back_to_test_automation(tmp_path):
    runtime = runtime_with(tmp_path, POLICY, preset("test-first"))  # the default gate always passes
    runner = Runner()
    result = runtime.execute("T", "run", runner=runner)
    assert result["state"] == "blocked"
    assert "developer" not in runner.calls and runner.calls.count("test-automation") >= 2
    findings = runtime.store.db.rows("SELECT payload_json FROM events WHERE aggregate_id='T' AND kind='FindingDetected'")
    assert "이미 모두 통과" in json.loads(findings[0]["payload_json"])["decision"]["root_cause"]
