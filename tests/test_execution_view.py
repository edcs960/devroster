"""Execution view model (EX-01): stages, current selection, timeline cursor, cost."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_dev.adapters import execution_view
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.contracts import PhaseResult

POLICY = {
    "currency": "USD",
    "alert_thresholds": [0.5, 0.9],
    "estimated_budget": 1.0,
    "price_book": [{"provider": "codex_cli", "model": "gpt-x", "input_per_million": 1.0, "cached_input_per_million": 0.1, "output_per_million": 1.0}],
}


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class Runner:
    def __init__(self, usage):
        self.usage = usage

    def execute(self, request):
        root = Path(json.loads(request.context)["repositories"]["source"]["worktree"])
        (root / "source.txt").write_text("implemented", encoding="utf-8")
        return PhaseResult(
            '```json\n{"handoff_status":"pass","summary":"private notes stay out"}\n```',
            provider="codex_cli",
            usage=self.usage,
            selection={"model": "gpt-x", "effort": "medium", "reason": "standard task", "private_reasoning": "chain-of-thought-marker"},
        )


@pytest.fixture
def runtime(tmp_path):
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
            "phases": [{"id": "developer", "capabilities": ["code.write"]}],
            "verification": {"commands": [{"name": "unit", "repository": "source", "command": [sys.executable, "-c", "print(1)"]}], "required_gates": ["unit"]},
            "usage_policy": POLICY,
        },
    )
    runtime.store.design("P", "D", "## Scope\nS\n## Acceptance\nA\n## Constraints\nC")
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan("P", "PLAN", "D", 1, [{"id": "T", "title": "Execution task"}])
    runtime.store.approve("plan", "PLAN", 1, "user")
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    return runtime


def test_view_before_execution_has_no_fabricated_progress(runtime):
    data = execution_view.view(runtime.root, "T")
    assert data["stages"]["total"] == 1 and data["stages"]["not_started"] == 1
    assert data["current"]["recorded"] is False and data["current"]["model"] is None
    assert data["cost"]["known_calls"] == 0 and data["cost"]["unknown_calls"] == 0
    assert data["terminal"] is False and data["poll"]["active_ms"] == 3000


def test_recorded_selection_cost_threshold_and_cursor(runtime):
    runtime.execute("T", "run-once", runner=Runner({"input_tokens": 600_000, "cached_input_tokens": 100_000, "output_tokens": 50_000}))
    data = execution_view.view(runtime.root, "T")
    assert data["stages"]["passed"] == 1
    assert data["current"]["model"] == "gpt-x" and data["current"]["effort"] == "medium"
    assert data["cost"]["known_calls"] == 1
    assert data["cost"]["known_cost"] == pytest.approx((500_000 * 1 + 100_000 * 0.1 + 50_000 * 1) / 1e6)
    text = json.dumps(data, ensure_ascii=False)
    assert "private notes" not in text and "chain-of-thought-marker" not in text
    events = [e["kind"] for e in data["timeline"]]
    assert "RunnerUsage" in events and events.count("UsageThresholdReached") == 1
    cursor = data["cursor"]
    again = execution_view.view(runtime.root, "T", cursor=cursor)
    assert again["timeline"] == [] and again["cursor"] == cursor
    assert again["current"]["model"] == "gpt-x" and again["current"]["reason"] == "standard task"
    assert runtime.store.record_usage_thresholds("T", POLICY) == []


def test_unmeasured_usage_is_unknown_not_zero(runtime):
    runtime.execute("T", "run-once", runner=Runner({}))
    data = execution_view.view(runtime.root, "T")
    assert data["cost"]["known_calls"] == 0 and data["cost"]["unknown_calls"] == 1
    assert data["cost"]["reasons"] == {"usage_unknown": 1}
    measurement = {m["metric"]: m for m in data["usage"]["measurements"]}
    assert measurement["input_tokens"]["value"] is None


def test_invalid_cursor_and_missing_task_are_rejected(runtime):
    from agent_dev.domain.errors import DomainError

    with pytest.raises(DomainError):
        execution_view.view(runtime.root, "T", cursor=-1)
    with pytest.raises(DomainError):
        execution_view.view(runtime.root, "missing")


def test_next_actions_are_korean_and_keep_unknown_codes():
    rows = execution_view.next_actions(
        [
            {"code": "VERIFICATION_PENDING", "message": "x", "action": "y"},
            {"code": "DEPENDENCY_PENDING", "message": "Dependencies are incomplete: A, B", "action": "z"},
            {"code": "CUSTOM", "message": "raw", "action": "raw action"},
        ],
        "T-1",
    )
    assert rows[0]["action"].endswith("(devroster task verify T-1)") and "검증" in rows[0]["message"]
    assert rows[1]["message"].endswith("A, B")
    assert rows[2] == {"code": "CUSTOM", "message": "raw", "action": "raw action"}


def test_redaction_masks_tokens_but_not_words_ending_in_sk():
    from agent_dev.domain.content import redact

    assert redact("task-verify-1791720712817211000") == "task-verify-1791720712817211000"
    assert redact("risk-assessment-module-v2") == "risk-assessment-module-v2"
    assert redact("key=sk-abcdefghijklmnop end") == "key=**** end"
    assert redact("Bearer sk-proj-ABCDEFGHIJKLMNOP") == "Bearer ****"
    assert redact('"ghp_ABCDEFGHIJKLMNOPQR"') == '"****"'
    assert redact("sk-abcdefghijklmnop") == "****"


def test_redaction_masks_tokens_after_escapes_and_encodings():
    import json as _json

    from agent_dev.domain.content import redact

    token = "sk-proj-ABCDEFGHIJKLMNOP"
    for text in (
        _json.dumps("line\n" + token),  # JSON-escaped newline
        _json.dumps("tab\t" + token),
        repr("log\n" + token),  # Python repr in test logs
        "key%3D" + token,  # URL-encoded '='
        "q=%22" + token + "%22",  # URL-encoded quotes
        '{"k":"\\u0022' + token + '"}',
        '"' + token + '"',
        "ghp_ABCDEFGHIJKLMNOPQRST at start",
        "x\\nghp_ABCDEFGHIJKLMNOPQRST",
    ):
        assert token not in redact(text) and "ghp_ABCDEFGHIJKLMNOPQRST" not in redact(text), text
    for word in ("task-verify-1791720712817211000", "risk-assessment-module-v2", "desk-organizer-items-2"):
        assert redact(word) == word
