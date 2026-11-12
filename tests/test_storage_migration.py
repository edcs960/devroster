"""Copy -> verify -> switch moves of a workspace or the management root, with recovery."""

import json
import os
from pathlib import Path

import pytest

pytest_plugins = ["test_workflow_runtime"]

from test_refactor_migration import legacy
from test_reusable_workspace import Runner, approved, workspace  # noqa: F401
from test_workflow_dag import prepare
from test_workflow_session_context import graph as session_graph

from agent_dev.adapters import storage_migration, storage_registry
from agent_dev.adapters.environment import read_environment
from agent_dev.adapters.global_settings import GlobalSettings
from agent_dev.adapters.storage_migration import apply_move, plan_move, recover_move
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.adapters.workspace import configuration_root
from agent_dev.adapters.workspace_locator import workspace_locations
from agent_dev.application.studio import Studio
from agent_dev.core import cli_home
from agent_dev.core.storage_locations import EXTERNAL, bootstrap_file, management_root
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.migration.legacy import collect_sources, import_sources

SECRET = "workspace-private-value"


@pytest.fixture
def prepared(workspace):  # noqa: F811
    """A project-local workspace with an executed task, Studio data, a role and a secret."""
    runtime = approved(workspace)
    runtime.execute("T", "run", runner=Runner())
    runtime.execute("T", "verify", verify=True)
    managed = workspace / ".agent-dev"
    (managed / "roles").mkdir()
    (managed / "roles/qa.md").write_text("# Local QA rules\n", encoding="utf-8")
    (managed / "environment.json").write_text(json.dumps({"APP_TOKEN": SECRET}), encoding="utf-8")
    Studio(StudioStore(managed), None).create("Idea", "Studio document")
    return workspace, runtime


def rows(db, query):
    return db.rows(query)


def evidence(runtime):
    return {
        "snapshots": rows(runtime.store.db, "SELECT id,digest,resolved_json FROM input_snapshots"),
        "approvals": rows(runtime.store.db, "SELECT * FROM approvals ORDER BY id"),
        "profiles": rows(
            runtime.store.db, "SELECT project_id,version,digest FROM project_profile_versions"
        ),
        "artifacts": rows(runtime.store.db, "SELECT path,digest FROM artifacts ORDER BY path"),
    }


def test_plan_is_read_only_and_apply_switches_after_verification(prepared, tmp_path):
    source, runtime = prepared
    before = evidence(runtime)
    target = tmp_path / "managed-config"
    registry_before = storage_registry.read()
    plan = plan_move(source / ".agent-dev", target, scope="workspace")
    assert plan["ready"], plan["checks"]
    assert storage_registry.read() == registry_before
    assert not target.exists()
    assert SECRET not in json.dumps(plan)
    result = apply_move(plan["plan_id"], expected_registry_revision=registry_before["revision"])
    assert result["state"] == "completed"
    assert result["restart_required"] is True
    assert (source / ".agent-dev/state.db").exists()  # originals are never deleted
    locations = workspace_locations(source)
    assert locations.layout == EXTERNAL and locations.config_root == target
    assert locations.workspace_id == plan["workspace_id"]
    assert configuration_root(source) == target
    moved = WorkflowRuntime(source)
    assert moved.store.db.path == target / "state.db"
    assert evidence(moved) == before
    assert moved.store.approved("T")
    assert moved.flags() == runtime.flags()
    assert read_environment(source) == {"APP_TOKEN": SECRET}
    assert locations.environment_file.stat().st_mode & 0o077 == 0
    assert (locations.role_dir / "qa.md").read_text(encoding="utf-8") == "# Local QA rules\n"
    assert [p["title"] for p in StudioStore(target).projects()] == ["Idea"]
    for row in before["artifacts"]:
        assert (locations.output_root / row["path"]).is_file()
    journal = storage_migration.journal(result["migration_id"])
    assert journal["state"] == "completed"
    assert SECRET not in json.dumps(journal)


def test_new_snapshot_keeps_role_text_and_digest_after_externalization(runtime, tmp_path):
    roles = runtime.project_root / ".agent-dev/roles"
    roles.mkdir(parents=True)
    (roles / "planner.md").write_text("# Team Planner\n\nOVERRIDE-RULE", encoding="utf-8")
    prepare(runtime, session_graph())
    original = runtime.store.snapshot("DAG")["resolved"]
    plan = plan_move(runtime.root, tmp_path / "managed", scope="workspace")
    assert plan["ready"], plan["checks"]
    apply_move(plan["plan_id"], expected_registry_revision=0)
    moved = WorkflowRuntime(runtime.root).resolve_snapshot("DAG")
    assert moved["role_context"]["planner"] == original["role_context"]["planner"]
    assert (
        moved["role_sources"]["planner"]["digest"] == original["role_sources"]["planner"]["digest"]
    )
    assert moved["role_sources"]["planner"]["path"] == "roles/planner.md"
    assert moved["role_sources"]["planner"]["location"] == "config"
    # The approved snapshot itself is unchanged.
    assert WorkflowRuntime(runtime.root).store.snapshot("DAG")["resolved"] == original


@pytest.mark.parametrize("case", ["lease", "same", "nested", "existing", "disk", "process"])
def test_unsafe_moves_are_not_ready_and_change_nothing(prepared, tmp_path, monkeypatch, case):
    source, runtime = prepared
    target = tmp_path / "managed"
    if case == "lease":
        with runtime.store.db.transaction() as sql:
            sql.execute("INSERT INTO leases VALUES('task:busy','other',1,9e12,0)")
    elif case == "same":
        target = source / ".agent-dev"
    elif case == "nested":
        target = source / "src/config"
    elif case == "existing":
        target.mkdir()
    elif case == "disk":
        usage = type("Usage", (), {"free": 10})()
        monkeypatch.setattr(storage_migration.shutil, "disk_usage", lambda path: usage)
    elif case == "process":
        data = source / ".agent-dev/data"
        data.mkdir(exist_ok=True)
        (data / "process_registry.json").write_text(
            json.dumps({"processes": [{"pid": os.getpid(), "task_id": "T"}]}), encoding="utf-8"
        )
    plan = plan_move(source, target, scope="workspace")
    assert plan["ready"] is False
    with pytest.raises(DomainError):
        apply_move(plan["plan_id"], expected_registry_revision=0)
    assert storage_registry.read()["revision"] == 0
    assert configuration_root(source) == source / ".agent-dev"


def test_stale_registry_revision_and_concurrent_apply_are_rejected(prepared, tmp_path):
    source, _ = prepared
    plan = plan_move(source, tmp_path / "managed", scope="workspace")
    with pytest.raises(DomainError) as stale:
        apply_move(plan["plan_id"], expected_registry_revision=7)
    assert stale.value.code == ErrorCode.STALE_VERSION
    lock = storage_migration.maintenance_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    with pytest.raises(DomainError) as busy:
        apply_move(plan["plan_id"], expected_registry_revision=0)
    assert busy.value.code == ErrorCode.LEASE_CONFLICT
    lock.unlink()
    assert apply_move(plan["plan_id"], expected_registry_revision=0)["state"] == "completed"
    with pytest.raises(DomainError):  # a plan is applied once
        apply_move(plan["plan_id"], expected_registry_revision=1)


def test_verification_failure_keeps_original_and_resume_completes(prepared, tmp_path, monkeypatch):
    source, _ = prepared
    target = tmp_path / "managed"
    plan = plan_move(source, target, scope="workspace")

    def broken(*args, **kwargs):
        raise DomainError(ErrorCode.VERIFICATION_FAILED, "injected verification failure")

    monkeypatch.setattr(storage_migration, "_verify", broken)
    with pytest.raises(DomainError):
        apply_move(plan["plan_id"], expected_registry_revision=0)
    journal = storage_migration.journal(plan["plan_id"])
    assert journal["state"] == "failed"
    assert not target.exists()
    assert configuration_root(source) == source / ".agent-dev"
    monkeypatch.undo()
    recovered = recover_move(plan["plan_id"], action="resume")
    assert recovered["state"] in {"completed", "rollback_required"}
    assert recovered["state"] == "completed"
    assert configuration_root(source) == target


def test_disk_error_during_copy_fails_cleanly(prepared, tmp_path, monkeypatch):
    source, _ = prepared
    target = tmp_path / "managed"
    plan = plan_move(source, target, scope="workspace")

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage_migration, "_sqlite_copy", full)
    with pytest.raises(OSError):
        apply_move(plan["plan_id"], expected_registry_revision=0)
    assert storage_migration.journal(plan["plan_id"])["state"] == "failed"
    assert not target.exists()
    assert not list(tmp_path.glob(".managed.staging-*"))
    assert configuration_root(source) == source / ".agent-dev"
    monkeypatch.undo()
    assert recover_move(plan["plan_id"], action="rollback")["state"] == "rolled_back"


def interrupted_before_switch(prepared, tmp_path, monkeypatch):
    source, _ = prepared
    target = tmp_path / "managed"
    plan = plan_move(source, target, scope="workspace")

    def crash(*args, **kwargs):
        raise KeyboardInterrupt  # the process stops after the copy was installed

    monkeypatch.setattr(storage_migration, "_switch", crash)
    with pytest.raises(KeyboardInterrupt):
        apply_move(plan["plan_id"], expected_registry_revision=0)
    monkeypatch.undo()
    assert storage_migration.journal(plan["plan_id"])["state"] == "verified"
    assert target.exists() and configuration_root(source) == source / ".agent-dev"
    return source, target, plan


def test_interrupted_switch_resumes(prepared, tmp_path, monkeypatch):
    source, target, plan = interrupted_before_switch(prepared, tmp_path, monkeypatch)
    assert recover_move(plan["plan_id"], action="resume")["state"] == "completed"
    assert configuration_root(source) == target


def test_interrupted_switch_rolls_back_without_touching_the_original(
    prepared, tmp_path, monkeypatch
):
    source, target, plan = interrupted_before_switch(prepared, tmp_path, monkeypatch)
    assert recover_move(plan["plan_id"], action="rollback")["state"] == "rolled_back"
    assert not target.exists()
    assert configuration_root(source) == source / ".agent-dev"
    assert WorkflowRuntime(source).store.approved("T")


def test_rollback_after_switch_is_refused_once_the_new_copy_was_written(prepared, tmp_path):
    source, _ = prepared
    target = tmp_path / "managed"
    plan = plan_move(source, target, scope="workspace")
    apply_move(plan["plan_id"], expected_registry_revision=0)
    WorkflowRuntime(source).store.event("EXAMPLE", "WrittenAfterSwitch", {})
    with pytest.raises(DomainError):
        recover_move(plan["plan_id"], action="rollback")
    assert storage_migration.journal(plan["plan_id"])["state"] == "rollback_required"
    assert configuration_root(source) == target  # the current DB stays in use


def test_rollback_after_switch_without_new_writes_reverses_the_pointer(prepared, tmp_path):
    source, _ = prepared
    target = tmp_path / "managed"
    plan = plan_move(source, target, scope="workspace")
    apply_move(plan["plan_id"], expected_registry_revision=0)
    assert recover_move(plan["plan_id"], action="rollback")["state"] == "rolled_back"
    assert configuration_root(source) == source / ".agent-dev"
    assert target.exists()  # the switched copy is retained, never deleted


def test_legacy_engine_layout_keeps_flags_and_zero_diff_compare(tmp_path, monkeypatch):
    (tmp_path / "engine").mkdir()
    root = legacy(tmp_path / "engine")
    # An idle legacy task; a task left in verifying counts as active work, as for backups.
    tasks = {"tasks": [{"id": "T1", "title": "Task", "status": "done", "depends_on": []}]}
    (root / "data/backlog.json").write_text(json.dumps(tasks), encoding="utf-8")
    (root / "config").mkdir()
    flags = {"read": True, "dual_write": True, "execute": True, "publish": False}
    (root / "config/runtime.json").write_text(json.dumps({"orchestrator_v2": flags}), encoding="utf-8")
    runtime = WorkflowRuntime(root)
    import_sources(runtime.store, collect_sources(root, root / "data"), dry_run=False)
    target = tmp_path / "managed"
    plan = plan_move(root, target, scope="workspace")
    assert plan["ready"], plan["checks"]
    result = apply_move(plan["plan_id"], expected_registry_revision=0)
    checks = {row["name"]: row for row in result["checks"]}
    assert checks["orchestrator_v2"]["status"] == "pass"
    assert checks["refactor_compare"]["status"] == "pass"
    moved = WorkflowRuntime(root)
    assert moved.flags() == flags
    assert moved.store.db.path == target / "state.db"


def test_management_root_move_switches_the_bootstrap_pointer(tmp_path, monkeypatch, prepared):
    source, _ = prepared
    old = management_root()
    GlobalSettings().save({"ai": {"model": "m"}}, expected_revision=0)
    plan = plan_move(source, old / "projects/one", scope="workspace")
    apply_move(plan["plan_id"], expected_registry_revision=0)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    monkeypatch.setattr(cli_home.sys, "platform", "darwin")
    monkeypatch.delenv("DEVROSTER_HOME")
    pointer = bootstrap_file({})
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"schema_version": 1, "management_root": str(old)}), encoding="utf-8")
    assert management_root() == old
    destination = tmp_path / "new-management"
    revision = storage_registry.read()["revision"]
    plan = plan_move(old, destination, scope="management")
    assert plan["ready"], plan["checks"]
    result = apply_move(plan["plan_id"], expected_registry_revision=revision)
    assert result["state"] == "completed" and result["restart_required"]
    assert management_root() == destination
    assert old.exists()
    assert GlobalSettings().read()["values"] == {"ai": {"model": "m"}}
    moved = workspace_locations(source)
    assert moved.config_root == destination / "projects/one"
    assert read_environment(source) == {"APP_TOKEN": SECRET}
    assert WorkflowRuntime(source).store.approved("T")
    assert recover_move(result["migration_id"], action="rollback")["state"] == "rolled_back"
    assert management_root() == old


def test_management_move_refuses_an_explicit_environment_root(tmp_path):
    plan = plan_move(management_root(), tmp_path / "new", scope="management")
    assert plan["ready"] is False
    assert any(row["name"] == "environment_override" for row in plan["checks"])


def test_unknown_scope_and_migration_ids_are_rejected(tmp_path):
    with pytest.raises(DomainError):
        plan_move(tmp_path, tmp_path / "x", scope="everything")
    with pytest.raises(DomainError):
        apply_move("../escape", expected_registry_revision=0)
    with pytest.raises(DomainError):
        recover_move("missing", action="resume")
    with pytest.raises(DomainError):
        recover_move("missing", action="delete")
