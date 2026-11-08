"""P8 automated gate: two projects with the same business project_id, two management roots.

Uses only the public P1-P7 contracts. The AI runner is a local fake, so this is not a live
CLI E2E; real Claude/Codex runs and OS-specific checks are recorded as Not Tested (local).
"""

import json
import subprocess
from pathlib import Path

import pytest
from test_reusable_workspace import Runner, approved, git
from test_shared_integrations import FakeClient

from agent_dev.adapters.backup import create_bundle, restore_bundle
from agent_dev.adapters.credential_store import CredentialStore
from agent_dev.adapters.global_settings import GlobalSettings
from agent_dev.adapters.integrations import Integrations
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.shared_integrations import SharedIntegrations
from agent_dev.adapters.storage_migration import apply_move, journal, plan_move, recover_move
from agent_dev.adapters.storage_registry import read as registry
from agent_dev.adapters.storage_registry import workspace_locations
from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.adapters.workspace import initialize
from agent_dev.core import cli_home
from agent_dev.core.storage_locations import EXTERNAL, bootstrap_file, management_root
from agent_dev.domain.content import content_digest
from agent_dev.domain.errors import DomainError

TOKEN = "e2e-shared-token-value"


def project(root):
    (root / "src").mkdir(parents=True)
    (root / "src/source.txt").write_text("baseline", encoding="utf-8")
    (root / ".gitignore").write_text("/.agent-dev/\n/output/\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "add", "--", "src/source.txt", ".gitignore")
    git(root, "commit", "-qm", "baseline")
    # Both projects share the business project_id on purpose.
    initialize(root, name="Example", project_id="EXAMPLE", paths=["src"])
    return root


def evidence(runtime):
    db = runtime.store.db
    return {
        "snapshots": db.rows("SELECT id,digest FROM input_snapshots ORDER BY id"),
        "approvals": db.rows("SELECT * FROM approvals ORDER BY id"),
        "artifacts": db.rows("SELECT path,digest FROM artifacts ORDER BY path"),
    }


def status(root):
    return subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    ).stdout


def run_new_task(runtime, task_id):
    runtime.store.create_plan(
        "EXAMPLE",
        "PLAN-" + task_id,
        "D",
        1,
        [{"id": task_id, "title": task_id, "system_name": "Example"}],
    )
    runtime.store.approve("plan", "PLAN-" + task_id, 1, "user")
    runtime.store.create_snapshot(task_id, runtime.resolve_snapshot(task_id))
    assert runtime.execute(task_id, "run-" + task_id, runner=Runner())["state"] == "verifying"
    assert runtime.execute(task_id, "verify-" + task_id, verify=True)["passed"]
    with runtime.safety.lease("task:" + task_id) as lease:
        checkpoint = Path(runtime.artifacts.checkpoint(task_id, lease, reason="e2e checkpoint"))
    assert checkpoint.is_file()
    for row in runtime.store.db.rows(
        "SELECT path,digest FROM artifacts WHERE task_id=?", (task_id,)
    ):
        path = runtime.locations.output_root / row["path"]
        assert content_digest(path.read_bytes()) == row["digest"]


def test_end_to_end_settings_management(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_dev.adapters.integrations.remote_address", lambda *a, **k: None)
    a, b = project(tmp_path / "alpha"), project(tmp_path / "beta")
    runtime_a, runtime_b = approved(a), approved(b)
    runtime_a.execute("T", "run", runner=Runner())
    runtime_a.execute("T", "verify", verify=True)
    original_a, original_b = evidence(runtime_a), evidence(runtime_b)
    source_status = status(a)

    # Shared defaults reach A only through an explicit new profile; snapshots stay as approved.
    shared = GlobalSettings().save({"ai": {"model": "e2e-model"}}, expected_revision=0)
    setup = ProjectSetup(a)
    setup.apply_global(shared["revision"], expected_revision=setup.read()["revision"])
    assert ProjectSetup(a).effective()["origins"]["ai.model"] == "global"
    assert ProjectSetup(b).effective()["applied"] is None
    assert evidence(WorkflowRuntime(a)) == original_a

    # A shared MCP connection is unusable until A binds it, and B never sees A's binding.
    connections = SharedIntegrations()
    connections.register(
        "github-shared",
        {"provider": "github", "scopes": ["repo-A", "repo-B"], "credential_ref": "GITHUB_TOKEN"},
        expected_version=0,
    )
    CredentialStore.save_shared("github-shared", "GITHUB_TOKEN", TOKEN)
    SharedIntegrations(a).bind("", "github", "github-shared", 1, ["repo-A"], ["get_me"], 0)
    app_a = Integrations(a, client_factory=FakeClient)
    assert app_a.check("github", operation="get_me", scope="repo-A")["state"] == "ready"
    assert Integrations(b).list() == []

    # Externalize A: copy -> verify -> switch; the source checkout and B are untouched.
    target = management_root() / "projects" / "alpha"
    plan = plan_move(a, target, scope="workspace")
    assert plan["ready"], plan["checks"]
    moved = apply_move(plan["plan_id"], expected_registry_revision=registry()["revision"])
    assert moved["state"] == "completed"
    locations_a, locations_b = workspace_locations(a), workspace_locations(b)
    assert locations_a.layout == EXTERNAL and locations_a.state_db == target / "state.db"
    assert locations_a.state_db != locations_b.state_db
    assert (a / ".agent-dev/state.db").exists()  # original retained
    runtime_a = WorkflowRuntime(a)
    assert evidence(runtime_a) == original_a
    assert runtime_a.flags()["execute"] is True
    assert evidence(WorkflowRuntime(b)) == original_b
    assert status(a) == source_status

    # The externalized workspace keeps running: shared binding, new task, gate, checkpoint.
    assert Integrations(a, client_factory=FakeClient).call("github", "get_me", {}, scope="repo-A")
    run_new_task(runtime_a, "T2")
    assert TaskQueries(a).explain("T2")["approvals"] == {
        "design": True,
        "plan": True,
        "snapshot": True,
    }
    assert status(a) == source_status

    # A portable bundle of the external workspace restores as a new, disabled workspace.
    bundle = tmp_path / "alpha.zip"
    create_bundle(a, bundle)
    restored = WorkflowRuntime(restore_bundle(bundle, tmp_path / "restored")["restored"])
    assert restored.store.snapshot("T")["digest"] == original_a["snapshots"][0]["digest"]
    assert not restored.store.approved("T")
    assert TOKEN.encode() not in bundle.read_bytes()

    # Move the management root A -> B; projects, shared settings and credentials follow.
    old = management_root()
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    monkeypatch.setattr(cli_home.sys, "platform", "darwin")
    monkeypatch.delenv("DEVROSTER_HOME")
    pointer = bootstrap_file({})
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"schema_version": 1, "management_root": str(old)}), encoding="utf-8")
    plan = plan_move(old, tmp_path / "management-b", scope="management")
    assert plan["ready"], plan["checks"]
    result = apply_move(plan["plan_id"], expected_registry_revision=registry()["revision"])
    assert result["state"] == "completed" and management_root() == tmp_path / "management-b"
    runtime_a = WorkflowRuntime(a)
    assert runtime_a.store.db.path == tmp_path / "management-b/projects/alpha/state.db"
    assert runtime_a.store.approved("T2")
    assert GlobalSettings().read()["values"] == {"ai": {"model": "e2e-model"}}
    assert Integrations(a, client_factory=FakeClient).call("github", "get_me", {}, scope="repo-A")
    assert FakeClient.calls[-1] == ("get_me", TOKEN)
    assert evidence(WorkflowRuntime(b)) == original_b
    # The new copy was written (the shared call above), so a simple rollback is refused.
    with pytest.raises(DomainError):
        recover_move(result["migration_id"], action="rollback")
    assert journal(result["migration_id"])["state"] == "rollback_required"
    assert management_root() == tmp_path / "management-b" and old.exists()
