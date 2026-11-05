"""ADR 0002 W2: workspace role overrides and one context per session."""

import json
import os
from itertools import pairwise

import pytest

pytest_plugins = ["test_workflow_runtime"]

from test_workflow_dag import prepare

from agent_dev.domain import session_output
from agent_dev.domain.errors import DomainError, ErrorCode


def graph():
    nodes = [
        {"id": "planner", "kind": "role", "role_id": "planner", "session": "analysis"},
        {"id": "architect", "kind": "role", "role_id": "architect", "session": "analysis"},
        {"id": "developer", "kind": "role", "role_id": "developer"},
    ]
    return {
        "schema_version": 1,
        "id": "w2",
        "name": "W2",
        "nodes": nodes,
        "edges": [{"from": a["id"], "to": b["id"]} for a, b in pairwise(nodes)],
    }


def roles_dir(runtime):
    directory = runtime.project_root / ".agent-dev/roles"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def resolved(runtime):
    return runtime.store.snapshot("DAG")["resolved"]


def test_built_in_roles_without_override_record_no_source(runtime):
    prepare(runtime, graph())
    value = resolved(runtime)
    assert "role_sources" not in value
    assert "role_source" not in json.loads(runtime.artifacts.context("DAG", "planner"))


def test_workspace_override_replaces_built_in_and_is_recorded(runtime):
    (roles_dir(runtime) / "planner.md").write_text("# Team Planner\n\nOVERRIDE-RULE")
    # Repository rule folders stay ignored even when present.
    (runtime.project_root / "agent").mkdir()
    (runtime.project_root / "agent/architect.md").write_text("REPO-RULE")
    prepare(runtime, graph())
    value = resolved(runtime)
    assert value["role_context"]["planner"].startswith("# Team Planner\n\nOVERRIDE-RULE")
    assert "REPO-RULE" not in value["role_context"]["architect"]
    source = value["role_sources"]["planner"]
    assert source["source"] == "override"
    assert source["path"] == ".agent-dev/roles/planner.md"
    assert set(value["role_sources"]) == {"planner"}
    assert json.loads(runtime.artifacts.context("DAG", "planner"))["role_source"] == source


def test_override_change_changes_resolved_snapshot(runtime):
    override = roles_dir(runtime) / "planner.md"
    override.write_text("FIRST")
    prepare(runtime, graph())
    first = runtime.resolve_snapshot("DAG")
    override.write_text("SECOND")
    second = runtime.resolve_snapshot("DAG")
    assert first["role_sources"]["planner"]["digest"] != second["role_sources"]["planner"]["digest"]
    assert "SECOND" in second["role_context"]["planner"]


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (
            lambda d: os.symlink(d.parent / "elsewhere.md", d / "planner.md"),
            ErrorCode.POLICY_VIOLATION,
        ),
        (lambda d: (d / "planner.md").mkdir(), ErrorCode.POLICY_VIOLATION),
        (lambda d: (d / "planner.md").write_bytes(b"x" * (64 * 1024 + 1)), ErrorCode.INVALID_INPUT),
        (lambda d: (d / "planner.md").write_bytes(b"\xff\xfe"), ErrorCode.INVALID_INPUT),
    ],
)
def test_unsafe_overrides_are_rejected(runtime, setup, code):
    directory = roles_dir(runtime)
    (directory.parent / "elsewhere.md").write_text("OUTSIDE")
    setup(directory)
    with pytest.raises(DomainError) as error:
        prepare(runtime, graph())
    assert error.value.code == code


def test_session_context_shares_common_keys_once(runtime):
    prepare(runtime, graph())
    value = json.loads(
        runtime.artifacts.session_context("DAG", "analysis", ["planner", "architect"])
    )
    assert value["session_id"] == "analysis"
    assert value["operating_rules"] and value["task"]
    assert "phase_id" not in value and "role_instruction" not in value
    assert [n["phase_id"] for n in value["nodes"]] == ["planner", "architect"]
    assert value["nodes"][0]["role_instruction"] != value["nodes"][1]["role_instruction"]
    assert all("operating_rules" not in n for n in value["nodes"])
    assert "<<<NODE planner>>>" in value["output_contract"]


def test_split_returns_each_node_section():
    output = (
        "preamble\n<<<NODE planner>>>\nplan\n"
        '```json\n{"handoff_status":"pass"}\n```\n'
        "<<<END NODE planner>>>\n<<<NODE architect>>>\ndesign without end"
    )
    sections = session_output.split(output, ["planner", "architect", "dba"])
    assert sections["planner"].startswith("plan") and sections["planner"].endswith("```")
    assert sections["architect"] is None
    assert sections["dba"] is None


def test_split_reads_sections_in_order():
    output = (
        "<<<NODE planner>>>\nquote: <<<NODE architect>>> fake <<<END NODE architect>>>\n"
        "<<<END NODE planner>>>\n<<<NODE architect>>>\nreal\n<<<END NODE architect>>>"
    )
    sections = session_output.split(output, ["planner", "architect"])
    assert sections["architect"] == "real"
    assert sections["planner"].startswith("quote:")


def test_session_context_serializes_shared_wiki_items_once(runtime):
    root = runtime.project_root
    (root / "lesson.md").write_text("## Resolution\n- Resolved: true\n")

    def item(item_id, roles, check):
        return {
            "item_id": item_id,
            "page": "lessons",
            "title": item_id,
            "check": check,
            "roles": roles,
            "sources": ["lesson.md"],
        }

    runtime.wiki.propose(
        "P",
        [
            item("L10", ["all"], "SHARED-CHECK"),
            item("L01", ["planner"], "PLANNER-CHECK"),
            item("L02", ["architect"], "ARCHITECT-CHECK"),
        ],
    )
    for item_id in ("L10", "L01", "L02"):
        runtime.wiki.approve("P", item_id, 1, "u")
    prepare(runtime, graph())
    raw = runtime.artifacts.session_context("DAG", "analysis", ["planner", "architect"])
    value = json.loads(raw)
    assert raw.count("SHARED-CHECK") == 1
    assert sorted(value["wiki_items"]) == ["L01 r1", "L02 r1", "L10 r1"]
    assert [n["wiki_item_ids"] for n in value["nodes"]] == [
        ["L01 r1", "L10 r1"],
        ["L02 r1", "L10 r1"],
    ]
    assert all("wiki_verified_data" not in n for n in value["nodes"])
    resumed = json.loads(session_output.resumed_context(raw))
    assert "wiki_items" not in resumed
    assert all("wiki_item_ids" not in n for n in resumed["nodes"])
