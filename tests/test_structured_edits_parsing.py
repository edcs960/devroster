"""Structured edits whose content holds Markdown code fences (found in a Claude run)."""

import json

import pytest

from agent_dev.adapters.phase_catalog import SnapshotPhaseCatalog, json_blocks

DOC = '# Usage\n\n```python\ngreet(" Ada ")  # "Hello, Ada!"\n```\n'
EDITS = json.dumps({"edits": [{"path": "docs/usage.md", "content": DOC}]}, ensure_ascii=False)
CONTROL = '{"handoff_status":"pass","summary":"docs updated"}'
OUTPUT = f"## 작업 요약\n- docs\n\n```json\n{EDITS}\n```\n\n```json\n{CONTROL}\n```\n"


def test_json_blocks_keep_code_fences_inside_strings():
    blocks = json_blocks(OUTPUT)
    assert blocks[0]["edits"][0]["content"] == DOC
    assert blocks[1]["handoff_status"] == "pass"


def test_unreadable_edits_fail_instead_of_passing(runtime, tmp_path):
    from test_workflow_dag import prepare

    from agent_dev.domain.errors import DomainError

    prepare(
        runtime,
        {
            "schema_version": 1,
            "id": "docs",
            "name": "Docs",
            "nodes": [{"id": "tech-writer", "kind": "role", "role_id": "tech-writer"}],
            "edges": [],
        },
    )
    broken = '```json\n{"edits": [{"path": "docs/usage.md", "content": "unterminated}\n```\n'
    with pytest.raises(DomainError, match="Structured edits could not be parsed"):
        SnapshotPhaseCatalog(runtime.store, runtime.root).apply_role_result(
            "DAG", "tech-writer", broken
        )



def test_an_empty_proposal_is_read_as_no_change(runtime):
    """Test Automation answered `{"edits":[]}` in a real verify session and was failed."""
    from test_workflow_dag import prepare

    prepare(
        runtime,
        {
            "schema_version": 1,
            "id": "docs",
            "name": "Docs",
            "nodes": [{"id": "tech-writer", "kind": "role", "role_id": "tech-writer"}],
            "edges": [],
        },
    )
    output = '# 결과\n- 변경 제안 없음\n\n```json\n{"edits":[]}\n```\n\n```json\n' + CONTROL + "\n```\n"
    assert (
        SnapshotPhaseCatalog(runtime.store, runtime.root).apply_role_result(
            "DAG", "tech-writer", output
        )
        is None
    )



def test_structured_role_without_paths_reports_instead_of_blocking(runtime):
    """A new project without document paths made Tech Writer block the task."""
    from test_workflow_dag import prepare

    prepare(
        runtime,
        {
            "schema_version": 1,
            "id": "docs",
            "name": "Docs",
            "nodes": [{"id": "tech-writer", "kind": "role", "role_id": "tech-writer"}],
            "edges": [],
        },
    )
    context = json.loads(runtime.artifacts.context("DAG", "tech-writer"))
    assert "document_paths" in context["structured_scope"]
    assert '{"edits": []}' in context["structured_scope"]


pytest_plugins = ["test_workflow_runtime"]


@pytest.mark.parametrize(
    ("role", "key"), [("tech-writer", "document_paths"), ("test-automation", "test_paths")]
)
def test_missing_structured_paths_explain_what_to_configure(runtime, role, key):
    from test_workflow_dag import prepare

    from agent_dev.domain.errors import DomainError

    prepare(
        runtime,
        {
            "schema_version": 1,
            "id": "paths",
            "name": "Paths",
            "nodes": [{"id": role, "kind": "role", "role_id": role}],
            "edges": [],
        },
    )
    assert not runtime.store.snapshot("DAG")["resolved"].get(key)
    edits = json.dumps({"edits": [{"path": "docs/usage.md", "content": "x"}]})
    with pytest.raises(DomainError, match=key):
        SnapshotPhaseCatalog(runtime.store, runtime.root).apply_role_result(
            "DAG", role, f"```json\n{edits}\n```\n"
        )


def test_handoff_notes_never_land_in_the_project():
    """A Tech Writer proposal wrote docs/<TASK>/latest-checkpoint.md in an earlier run."""
    from agent_dev.adapters.phase_catalog import handoff_note

    assert handoff_note("docs/T-1/latest-checkpoint.md")
    assert handoff_note("docs/checkpoints/20261024-checkpoint.md")
    assert not handoff_note("docs/사용자가이드.md")
    assert not handoff_note("README.md")
