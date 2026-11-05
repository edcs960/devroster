import sqlite3

import pytest

from agent_dev.adapters.workflow_definitions import WorkflowDefinitions, preset
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.workflow_graph import normalize


@pytest.fixture
def service(tmp_path):
    runtime = WorkflowRuntime(tmp_path)
    runtime.store.register_project(
        "P",
        "Project",
        {
            "repositories": {"app": {"path": "."}},
            "phases": [{"id": "developer"}],
            "verification": {"commands": [{"name": "unit"}]},
        },
    )
    return WorkflowDefinitions(runtime=runtime)


def test_all_seven_presets_immutable_and_clone_versioned(service):
    assert len(service.presets()) == 7
    for graph in service.presets():
        normalize({k: v for k, v in graph.items() if k not in {"builtin", "version"}})
    with pytest.raises(DomainError):
        service.save("P", preset("quick-fix"))
    document = preset("quick-fix")
    document["id"] = "custom"
    saved = service.save("P", document)
    assert saved["version"] == 1
    document["name"] = "Updated"
    assert service.save("P", document, 1)["version"] == 2
    assert service.show("P", "custom", 1)["document"]["name"] != "Updated"
    with pytest.raises(DomainError) as conflict:
        service.save("P", document, 1)
    assert conflict.value.code == ErrorCode.STALE_VERSION
    with service.store.db.transaction() as sql, pytest.raises(sqlite3.IntegrityError):
        sql.execute("UPDATE workflow_versions SET document_json='{}'")


def test_apply_profile_keeps_original_and_import_remap(service):
    result = service.apply("P", "quick-fix", 0)
    assert result["profile_version"] == 2
    assert "workflow" not in __import__("json").loads(
        service.store.db.rows("SELECT content_json FROM project_profile_versions WHERE version=1")[
            0
        ]["content_json"]
    )
    document = service.export("P", "quick-fix")
    imported = service.import_document("P", document)
    assert imported["document"]["id"] == "quick-fix-copy"
    assert service.list("P") == []


def test_full_feature_impact_preview_runs_required_roles(service):
    result = service.preview(
        preset("full-feature"),
        {"title": "FMEA database migration", "workstreams": ["backend"]},
        "P",
    )
    assert result["missing_roles"] == []
    assert result["nodes"]["dba"]["state"] == "passed"
    assert result["nodes"]["fmea-expert"]["state"] == "passed"
    assert result["nodes"]["designer"]["state"] == "skipped"


def test_v1_database_migrates_preserving_existing_projects(tmp_path):
    from pathlib import Path

    from agent_dev.adapters.state.sqlite.database import Database

    path = tmp_path / "db.sqlite"
    old_schema = (
        Path(__file__)
        .parents[1]
        .joinpath("agent_dev/adapters/state/sqlite/schema.sql")
        .read_text()
        .split("CREATE TABLE IF NOT EXISTS workflow_definitions")[0]
    )
    sql = sqlite3.connect(path)
    sql.executescript(old_schema)
    sql.execute("INSERT INTO schema_versions VALUES(1,0)")
    sql.execute("INSERT INTO projects VALUES('old','old','old','draft',0,0)")
    sql.commit()
    sql.close()
    assert (
        not sqlite3.connect(path)
        .execute("SELECT name FROM sqlite_master WHERE name='workflow_node_runs'")
        .fetchone()
    )
    upgraded = Database(path)
    assert upgraded.rows("SELECT MAX(version) AS v FROM schema_versions")[0]["v"] == 4
    assert upgraded.rows("SELECT id FROM projects")[0]["id"] == "old"
    upgraded.backup(tmp_path / "backup.sqlite")
    assert (
        Database(tmp_path / "backup.sqlite", read_only=True).rows("SELECT id FROM projects")[0][
            "id"
        ]
        == "old"
    )
