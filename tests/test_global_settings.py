"""Versioned shared defaults, explicit project application and immutable snapshots."""

import json
import sqlite3
import threading

import pytest
from test_reusable_workspace import approved, workspace  # noqa: F401 - fixture reuse

from agent_dev.adapters.global_settings import GlobalSettings
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.core.storage_locations import management_root
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.project_roles import default_roles

SHARED = {"ai": {"provider": "claude_cli", "model": "shared-model"}, "limits": {"max_attempts": 7}}


def test_read_without_settings_creates_nothing(tmp_path):
    settings = GlobalSettings()
    assert settings.read() == {"revision": 0, "digest": settings.read()["digest"], "values": {}}
    assert not (management_root() / "global").exists()


def test_save_is_versioned_private_and_compare_and_swap(tmp_path):
    settings = GlobalSettings()
    first = settings.save(SHARED, expected_revision=0)
    assert first["revision"] == 1
    assert settings.read() == first
    assert settings.read(1) == first
    second = settings.save({"ai": {"model": "next"}}, expected_revision=1)
    assert second["revision"] == 2 and second["digest"] != first["digest"]
    assert settings.read(1)["values"] == SHARED  # past revisions stay readable
    with pytest.raises(DomainError) as stale:
        settings.save(SHARED, expected_revision=1)
    assert stale.value.code == ErrorCode.STALE_VERSION
    assert settings.path.stat().st_mode & 0o077 == 0
    assert settings.path.parent.stat().st_mode & 0o077 == 0
    with pytest.raises(DomainError):
        settings.read(99)


def test_past_revisions_and_events_are_immutable(tmp_path):
    settings = GlobalSettings()
    settings.save(SHARED, expected_revision=0)
    with sqlite3.connect(settings.path) as sql:
        for statement in (
            "UPDATE global_settings_versions SET content_json='{}'",
            "DELETE FROM global_settings_versions",
            "UPDATE global_events SET payload_json='{}'",
            "DELETE FROM global_events",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                sql.execute(statement)
        assert sql.execute("SELECT kind FROM global_events").fetchall() == [
            ("GlobalSettingsSaved",)
        ]


def test_concurrent_saves_of_one_revision_admit_exactly_one(tmp_path):
    settings = GlobalSettings()
    outcomes = []

    def save(model):
        try:
            outcomes.append(
                settings.save({"ai": {"model": model}}, expected_revision=0)["revision"]
            )
        except DomainError as error:
            outcomes.append(error.code)

    threads = [threading.Thread(target=save, args=(f"m{index}",)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(map(str, outcomes)) == sorted(["1", *[str(ErrorCode.STALE_VERSION)] * 3])
    assert settings.read()["revision"] == 1


@pytest.mark.parametrize(
    "values",
    [
        {"ai": {"provider": "unknown_cli"}},
        {"ai": {"model": None}},
        {"repositories": {"source": {"path": "."}}},
        {"limits": {"max_attempts": 0}},
    ],
)
def test_invalid_values_are_not_saved(tmp_path, values):
    settings = GlobalSettings()
    with pytest.raises(DomainError):
        settings.save(values, expected_revision=0)
    assert settings.read()["revision"] == 0


def test_newer_global_schema_is_refused(tmp_path):
    settings = GlobalSettings()
    settings.save(SHARED, expected_revision=0)
    with sqlite3.connect(settings.path) as sql:
        sql.execute("PRAGMA user_version=3")
    with pytest.raises(DomainError):
        GlobalSettings().read()


def profile_rows(root):
    return Database(root / ".agent-dev/state.db", read_only=True).rows(
        "SELECT version,digest,content_json FROM project_profile_versions ORDER BY version"
    )


def test_apply_global_creates_a_new_profile_with_provenance(workspace):  # noqa: F811
    shared = GlobalSettings().save(SHARED, expected_revision=0)
    setup = ProjectSetup(workspace)
    before = setup.read()
    effective = setup.effective()
    assert effective["values"]["ai"]["model"] == "shared-model"
    assert effective["origins"]["ai.model"] == "global"
    assert effective["global_revision"] == 1
    assert effective["applied"] is None and effective["pending_global"] is True
    assert len(profile_rows(workspace)) == 1  # reading never writes a profile
    result = setup.apply_global(shared["revision"], expected_revision=before["revision"])
    rows = profile_rows(workspace)
    assert [row["version"] for row in rows] == [1, 2]
    profile = json.loads(rows[-1]["content_json"])
    assert profile["runners"][0]["provider"] == "claude_cli"
    assert profile["runners"][0]["config"]["model"] == "shared-model"
    assert profile["runners"][0]["enabled"] is True
    assert profile["execution_limits"]["max_attempts"] == 7
    assert result["profile_version"] == 2
    provenance = Database(workspace / ".agent-dev/state.db", read_only=True).rows(
        "SELECT * FROM configuration_provenance"
    )
    assert provenance == [
        {
            "project_id": "EXAMPLE",
            "profile_version": 2,
            "global_revision": 1,
            "global_digest": shared["digest"],
            "override_json": "{}",
            "origins_json": provenance[0]["origins_json"],
        }
    ]
    assert json.loads(provenance[0]["origins_json"])["ai.model"] == "global"
    after = ProjectSetup(workspace).effective()
    assert after["applied"]["global_revision"] == 1 and after["pending_global"] is False
    GlobalSettings().save({"ai": {"model": "changed"}}, expected_revision=1)
    assert ProjectSetup(workspace).effective()["pending_global"] is True
    # A shared change never rewrites the project until it is applied again.
    assert len(profile_rows(workspace)) == 2


def test_override_and_inheritance_return(workspace):  # noqa: F811
    GlobalSettings().save(SHARED, expected_revision=0)
    setup = ProjectSetup(workspace)
    revision = setup.read()["revision"]
    setup.apply_global(1, expected_revision=revision, overrides={"ai": {"model": ""}})
    overridden = ProjectSetup(workspace).effective()
    assert overridden["values"]["ai"]["model"] == ""
    assert overridden["origins"]["ai.model"] == "project"
    assert overridden["overrides"] == {"ai": {"model": ""}}
    profile = json.loads(profile_rows(workspace)[-1]["content_json"])
    assert profile["runners"][0]["config"]["model"] == ""
    # Without new overrides the current ones are kept; deleting the key inherits again.
    revision = ProjectSetup(workspace).read()["revision"]
    ProjectSetup(workspace).apply_global(1, expected_revision=revision)
    assert ProjectSetup(workspace).effective()["overrides"] == {"ai": {"model": ""}}
    revision = ProjectSetup(workspace).read()["revision"]
    ProjectSetup(workspace).apply_global(1, expected_revision=revision, overrides={})
    assert ProjectSetup(workspace).effective()["origins"]["ai.model"] == "global"
    with pytest.raises(DomainError):
        ProjectSetup(workspace).apply_global(
            1, expected_revision=revision + 1, overrides={"ai": {"model": None}}
        )


def test_stale_project_revision_and_cap_are_rejected(workspace):  # noqa: F811
    GlobalSettings().save(
        {"limits": {"max_attempts": 3}, "limit_caps": {"max_attempts": 4}}, expected_revision=0
    )
    setup = ProjectSetup(workspace)
    revision = setup.read()["revision"]
    with pytest.raises(DomainError) as stale:
        setup.apply_global(1, expected_revision=revision + 5)
    assert stale.value.code == ErrorCode.STALE_VERSION
    with pytest.raises(DomainError):
        setup.apply_global(1, expected_revision=revision, overrides={"limits": {"max_attempts": 9}})
    assert len(profile_rows(workspace)) == 1


def test_templates_apply_only_when_requested(workspace):  # noqa: F811
    roster = default_roles()  # a template without session mode
    GlobalSettings().save({"templates": {"roles": roster}}, expected_revision=0)
    setup = ProjectSetup(workspace)
    before = setup.read()["roles"]
    assert before["session_mode"] is True  # new projects run sessions by default
    setup.apply_global(1, expected_revision=setup.read()["revision"])
    assert ProjectSetup(workspace).read()["roles"] == before
    setup = ProjectSetup(workspace)
    setup.apply_global(1, expected_revision=setup.read()["revision"], templates=True)
    assert "session_mode" not in ProjectSetup(workspace).read()["roles"]


def test_shared_change_leaves_approved_snapshot_bytes(workspace):  # noqa: F811
    runtime = approved(workspace)
    query = "SELECT digest,resolved_json FROM input_snapshots WHERE task_id='T'"
    original = runtime.store.db.rows(query)
    GlobalSettings().save(SHARED, expected_revision=0)
    setup = ProjectSetup(workspace)
    setup.apply_global(1, expected_revision=setup.read()["revision"])
    GlobalSettings().save({"ai": {"model": "other"}}, expected_revision=1)
    assert runtime.store.db.rows(query) == original
    assert runtime.store.approved("T")


def test_canonical_schema_v4_upgrade_backup_and_downgrade_refusal(tmp_path):
    path = tmp_path / "state.db"
    db = Database(path)
    assert db.rows("SELECT MAX(version) AS v FROM schema_versions")[0]["v"] == 4
    assert db.rows("SELECT name FROM sqlite_master WHERE name='configuration_provenance'")
    with sqlite3.connect(path) as sql:
        sql.execute("DELETE FROM schema_versions WHERE version=4")
    Database(path)
    assert len(list(tmp_path.glob("state.db.pre-v4-backup*"))) == 1
    with sqlite3.connect(path) as sql:
        sql.execute("INSERT INTO schema_versions VALUES(5,0)")
    with pytest.raises(DomainError):
        Database(path)


@pytest.mark.parametrize(
    "table", ["configuration_provenance", "shared_connection_bindings", "knowledge_revisions"]
)
def test_v4_backup_requires_every_v4_table(tmp_path, table):
    from agent_dev.adapters import backup

    path = tmp_path / "state.db"
    Database(path)
    assert backup.validate_database(path) == 4
    with sqlite3.connect(path) as sql:
        sql.execute(f"DROP TABLE {table}")
    with pytest.raises(DomainError):
        backup.validate_database(path)
