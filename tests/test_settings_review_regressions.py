"""PR #18 review findings (F1-F8): each test asserts the safe behavior."""

import json
import sqlite3
from pathlib import Path

import pytest

from agent_dev.adapters import storage_migration as m
from agent_dev.adapters import storage_registry as r
from agent_dev.adapters.credential_store import CredentialStore
from agent_dev.adapters.environment import read_environment
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.adapters.workspace import configuration_root, initialize
from agent_dev.core import cli_home
from agent_dev.core.storage_locations import bootstrap_file
from agent_dev.domain.errors import DomainError


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "management"))


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    initialize(root, name="Review fixture", project_id="REVIEW")
    return root


def move(source, target):
    plan = m.plan_move(source, target, "workspace")
    assert plan["ready"], plan["checks"]
    return plan, m.apply_move(plan["plan_id"], r.read()["revision"])


# -- F2 / F2b / F6: identity and location resolution ------------------------------------


def test_f2_unregistered_marker_cannot_reach_registered_credentials(source, tmp_path):
    plan, _ = move(source, tmp_path / "victim-config")
    victim = r.workspace_locations(source)
    victim.environment_file.parent.mkdir(parents=True, exist_ok=True)
    victim.environment_file.write_text(json.dumps({"VICTIM_TOKEN": "victim-secret-sentinel"}))
    attacker = tmp_path / "attacker-config"
    attacker.mkdir()
    (attacker / "workspace.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "project_id": "ATTACKER",
                "name": "Attacker",
                "workspace_id": plan["workspace_id"],
                "source_root": str(tmp_path / "attacker-source"),
            }
        )
    )
    assert r.find(attacker) is None
    locations = r.workspace_locations(attacker)
    assert locations.workspace_id == ""
    assert locations.environment_file.is_relative_to(attacker)
    assert "VICTIM_TOKEN" not in read_environment(attacker)
    store = CredentialStore(locations)
    for workspace_id in (plan["workspace_id"], ""):
        with pytest.raises(DomainError):
            store.resolve("project:VICTIM_TOKEN", workspace_id, "")


def test_f2b_workspace_id_never_escapes_the_credentials_root(tmp_path):
    config, outside = tmp_path / "untrusted", tmp_path / "outside"
    config.mkdir()
    outside.mkdir()
    (outside / "environment.json").write_text(json.dumps({"OUTSIDE_TOKEN": "escape-sentinel"}))
    (config / "workspace.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "project_id": "P",
                "name": "P",
                "workspace_id": str(outside),
                "source_root": str(tmp_path / "source"),
            }
        )
    )
    assert not r.workspace_locations(config).environment_file.is_relative_to(outside)
    assert "OUTSIDE_TOKEN" not in read_environment(config)
    # A registry entry with a path-like ID is rejected before it becomes a path.
    source = tmp_path / "registered"
    source.mkdir()
    state = r.state_file()
    state.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "source_root": str(source),
        "config_root": str(config),
        "runtime_root": str(source / ".agent-dev"),
    }
    state.write_text(
        json.dumps({"workspace_registry": {"revision": 1, "workspaces": {str(outside): row}}})
    )
    with pytest.raises(DomainError):
        r.workspace_locations(source)
    with pytest.raises(DomainError):
        r.register(source, tmp_path / "other", source / ".agent-dev", workspace_id="../escape")


def test_f6_missing_registered_config_is_an_error_without_a_new_db(source, tmp_path):
    target = tmp_path / "external"
    move(source, target)
    target.rename(tmp_path / "disconnected-config")
    with pytest.raises(DomainError):
        WorkflowRuntime(source)
    assert not target.exists()
    (tmp_path / "disconnected-config").rename(target)
    (target / "state.db").rename(tmp_path / "moved-away.db")
    with pytest.raises(DomainError):
        WorkflowRuntime(source)
    assert not (target / "state.db").exists()


# -- F3: shared caps bound every effective value ----------------------------------------


def test_f3_cap_bounds_inherited_existing_and_saved_limits(source):
    from agent_dev.adapters.global_settings import GlobalSettings
    from agent_dev.adapters.project_setup import ProjectSetup

    assert ProjectSetup(source).read()["limits"]["max_attempts"] > 1
    GlobalSettings().save({"limit_caps": {"max_attempts": 1}}, 0)
    effective = ProjectSetup(source).effective()
    assert effective["values"]["limits"]["max_attempts"] == 1
    assert effective["origins"]["limits.max_attempts"] == "global"
    setup = ProjectSetup(source)
    setup.apply_global(1, setup.read()["revision"])
    assert ProjectSetup(source).read()["limits"]["max_attempts"] == 1
    value = ProjectSetup(source).read()
    value["limits"]["max_attempts"] = 5
    with pytest.raises(DomainError):
        ProjectSetup(source).save(value)
    assert ProjectSetup(source).read()["limits"]["max_attempts"] == 1


# -- F1 / F4 / F4b / F5 / F5b / F7 / F8: copy -> verify -> switch and recovery ----------


def events(source, kind):
    return WorkflowRuntime(source).store.db.rows("SELECT id FROM events WHERE kind=?", (kind,))


def test_f1_rollback_after_unrecorded_switch_never_deletes_the_live_db(
    source, tmp_path, monkeypatch
):
    target = tmp_path / "external"
    plan = m.plan_move(source, target, "workspace")
    original = m._state

    def crash(record, state, **changes):
        if state == "switched":
            raise KeyboardInterrupt("after the registry switch, before the journal")
        return original(record, state, **changes)

    with monkeypatch.context() as patch:
        patch.setattr(m, "_state", crash)
        with pytest.raises(KeyboardInterrupt):
            m.apply_move(plan["plan_id"], 0)
    assert m.journal(plan["plan_id"])["state"] == "verified"
    assert configuration_root(source) == target
    WorkflowRuntime(source).store.event("REVIEW", "NewDataAfterSwitch", {})
    with pytest.raises(DomainError):
        m.recover_move(plan["plan_id"], "rollback")
    assert m.journal(plan["plan_id"])["state"] == "rollback_required"
    assert configuration_root(source) == target
    assert (target / "state.db").exists()
    assert events(source, "NewDataAfterSwitch")


def test_f1_resume_after_unrecorded_switch_completes_without_switching_again(
    source, tmp_path, monkeypatch
):
    target = tmp_path / "external"
    plan = m.plan_move(source, target, "workspace")
    original = m._state

    def crash(record, state, **changes):
        if state == "switched":
            raise KeyboardInterrupt
        return original(record, state, **changes)

    with monkeypatch.context() as patch:
        patch.setattr(m, "_state", crash)
        with pytest.raises(KeyboardInterrupt):
            m.apply_move(plan["plan_id"], 0)
    assert m.recover_move(plan["plan_id"], "resume")["state"] == "completed"
    assert configuration_root(source) == target


def interrupted_before_switch(source, tmp_path, monkeypatch):
    target = tmp_path / "external"
    plan = m.plan_move(source, target, "workspace")

    def crash(record):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(m, "_switch", crash)
        with pytest.raises(KeyboardInterrupt):
            m.apply_move(plan["plan_id"], 0)
    return target, plan


def test_f4_resume_refuses_a_lease_taken_after_the_plan(source, tmp_path, monkeypatch):
    _, plan = interrupted_before_switch(source, tmp_path, monkeypatch)
    with WorkflowRuntime(source).store.db.transaction() as conn:
        conn.execute("INSERT INTO leases VALUES('task:busy','live-worker',1,9e12,0)")
    with pytest.raises(DomainError):
        m.recover_move(plan["plan_id"], "resume")
    assert configuration_root(source) == source / ".agent-dev"
    assert WorkflowRuntime(source).store.db.rows("SELECT * FROM leases WHERE resource='task:busy'")


def test_f4_resume_recopies_an_original_written_after_verification(source, tmp_path, monkeypatch):
    target, plan = interrupted_before_switch(source, tmp_path, monkeypatch)
    WorkflowRuntime(source).store.event("REVIEW", "WrittenBeforeResume", {})
    assert m.recover_move(plan["plan_id"], "resume")["state"] == "completed"
    assert configuration_root(source) == target
    assert events(source, "WrittenBeforeResume")


def test_f4b_writers_are_blocked_until_the_switch(source, tmp_path, monkeypatch):
    plan = m.plan_move(source, tmp_path / "external", "workspace")
    original_db = source / ".agent-dev/state.db"
    switch = m._switch
    blocked = []

    def concurrent_write_then_switch(record):
        try:
            with sqlite3.connect(original_db, timeout=0.2) as writer:
                writer.execute(
                    "INSERT INTO events VALUES('concurrent','REVIEW',999,'Concurrent','{}',0)"
                )
        except sqlite3.OperationalError as error:
            blocked.append(str(error))
        return switch(record)

    monkeypatch.setattr(m, "_switch", concurrent_write_then_switch)
    assert m.apply_move(plan["plan_id"], 0)["state"] == "completed"
    assert blocked and "locked" in blocked[0]
    with sqlite3.connect(original_db) as sql:
        assert not sql.execute("SELECT 1 FROM events WHERE id='concurrent'").fetchall()


@pytest.mark.parametrize("folder", ["credentials/oops", "global/oops", "oops"])
def test_f5_targets_apply_would_refuse_are_never_ready(source, folder):
    target = r.management_root() / folder
    target.parent.mkdir(parents=True, exist_ok=True)
    plan = m.plan_move(source, target, "workspace")
    assert not plan["ready"]
    assert {c["name"] for c in plan["checks"] if c["status"] != "pass"} >= {"registry_mapping"}
    with pytest.raises(DomainError):
        m.apply_move(plan["plan_id"], 0)
    assert not target.exists()


def test_f5_a_failed_switch_leaves_nothing_installed(source, tmp_path, monkeypatch):
    target = tmp_path / "external"
    plan = m.plan_move(source, target, "workspace")

    def refused(*args, **kwargs):
        raise DomainError(m.ErrorCode.POLICY_VIOLATION, "registry refused")

    monkeypatch.setattr(m.storage_registry, "register", refused)
    with pytest.raises(DomainError):
        m.apply_move(plan["plan_id"], 0)
    assert not target.exists()
    assert m.journal(plan["plan_id"])["state"] == "failed"
    assert configuration_root(source) == source / ".agent-dev"


def management_pointer(tmp_path, monkeypatch, old):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    monkeypatch.setattr(cli_home.sys, "platform", "darwin")
    pointer = bootstrap_file({})
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps({"schema_version": 1, "management_root": str(old)}))
    monkeypatch.delenv("DEVROSTER_HOME")


def test_f5b_management_target_inside_a_registered_source_is_refused(source, tmp_path, monkeypatch):
    old = r.management_root()
    old.mkdir(parents=True, exist_ok=True)
    plan = m.plan_move(source, old / "projects" / "one", "workspace")
    m.apply_move(plan["plan_id"], 0)
    management_pointer(tmp_path, monkeypatch, old)
    for target in (source / "new-management", source / ".agent-dev" / "management"):
        plan = m.plan_move(old, target, "management")
        assert not plan["ready"]
        assert any(
            c["name"] == "registered_locations" and c["status"] == "fail" for c in plan["checks"]
        )
        with pytest.raises(DomainError):
            m.apply_move(plan["plan_id"], r.read()["revision"])
        assert not target.exists()
    assert r.workspace_locations(source).config_root == old / "projects" / "one"


def test_f7_update_only_write_after_switch_refuses_rollback(source, tmp_path):
    WorkflowRuntime(source).safety.cooldown("api", 1)
    plan, _ = move(source, tmp_path / "external")
    WorkflowRuntime(source).safety.cooldown("api", 999)
    with pytest.raises(DomainError):
        m.recover_move(plan["plan_id"], "rollback")
    assert m.journal(plan["plan_id"])["state"] == "rollback_required"
    assert configuration_root(source) == tmp_path / "external"


def test_f8_management_rollback_never_revives_a_revoked_credential(tmp_path, monkeypatch):
    from agent_dev.adapters.global_settings import GlobalSettings

    old = r.management_root()
    GlobalSettings().save({"ai": {"model": "review-model"}}, 0)
    CredentialStore.save_shared("docs", "DOC_TOKEN", "revoked-secret-sentinel")
    management_pointer(tmp_path, monkeypatch, old)
    target = tmp_path / "new-management"
    plan = m.plan_move(old, target, "management")
    assert plan["ready"], plan["checks"]
    m.apply_move(plan["plan_id"], 0)
    CredentialStore.revoke_shared("docs")
    with pytest.raises(DomainError):
        m.recover_move(plan["plan_id"], "rollback")
    assert r.management_root() == target
    assert "revoked-secret-sentinel" not in CredentialStore.shared_values()
