"""Prompt size per section is recorded for every AI call and shown, without the prompt text."""

import json

import pytest
from test_budget_pause import GRAPH, Runner, runtime_with

from agent_dev.adapters import execution_view
from agent_dev.domain.context_policy import profile


def test_sections_are_sized_largest_first_with_readable_names():
    context = json.dumps({"approved_design": "x" * 500, "task": {"id": "T"}, "phase_id": "developer"}, ensure_ascii=False)
    result = profile(context)
    assert result["total"] == len(context)
    assert [s["key"] for s in result["sections"]] == ["approved_design", "task", "phase_id"]
    assert result["sections"][0]["label"] == "승인된 설계" and result["sections"][0]["chars"] == 502
    assert profile("not json")["sections"] == []


@pytest.mark.parametrize("graph", [None, GRAPH], ids=["phases", "workflow-graph"])
def test_each_ai_call_records_its_prompt_profile_and_the_view_shows_the_latest(tmp_path, graph):
    runtime = runtime_with(tmp_path, {"estimated_budget": 100.0, "price_book": []}, graph)
    runtime.execute("T", "run-1", runner=Runner())
    rows = runtime.store.db.rows("SELECT payload_json FROM events WHERE aggregate_id='T' AND kind='RunnerUsage'")
    profiles = [json.loads(r["payload_json"])["context_chars"] for r in rows]
    assert profiles and all(p["total"] > 0 and p["sections"] for p in profiles)
    assert "repositories" in {s["key"] for s in profiles[0]["sections"]}
    assert len(profiles[0]["sections"]) <= 8
    view = execution_view.view(runtime.root, "T")
    assert view["prompt"]["total"] == profiles[-1]["total"]
    assert "## Scope" not in json.dumps(view["prompt"], ensure_ascii=False)  # sizes only, never text
