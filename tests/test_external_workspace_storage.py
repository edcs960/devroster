"""Every storage consumer follows the resolved locations of an external configuration."""

import json
import subprocess
from pathlib import Path

import pytest
from test_project_setup import config as setup_config

from agent_dev.adapters import storage_registry
from agent_dev.adapters.agent_console import AgentConsole
from agent_dev.adapters.design_references import DesignReferences
from agent_dev.adapters.diagnostics import diagnose
from agent_dev.adapters.environment import environment_file, read_environment
from agent_dev.adapters.integration_oauth import IntegrationOAuth
from agent_dev.adapters.integration_store import IntegrationStore
from agent_dev.adapters.integrations import Integrations
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.adapters.ui_queries import snapshot as ui_snapshot
from agent_dev.adapters.workflow_runtime import WorkflowRuntime, role_override
from agent_dev.adapters.workspace import initialize, metadata
from agent_dev.adapters.workspace_locator import workspace_locations
from agent_dev.domain.contracts import PhaseResult


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout.strip()


def repository(root):
    (root / "src").mkdir(parents=True)
    (root / "src/source.txt").write_text("baseline", encoding="utf-8")
    (root / ".gitignore").write_text("/.agent-dev/\n/output/\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.test")
    git(root, "add", "--", "src/source.txt", ".gitignore")
    git(root, "commit", "-qm", "baseline")


@pytest.fixture
def external(tmp_path):
    """A source folder whose settings and databases live in a separate folder."""
    source, managed = tmp_path / "source", tmp_path / "managed"
    repository(source)
    managed.mkdir()
    storage_registry.register(source, managed, source / ".agent-dev")
    value = setup_config(source)
    value.update(name="Example", project_id="EXAMPLE")
    value["environment"] = [{"key": "APP_TOKEN", "value": "external-private-value"}]
    ProjectSetup(source).save(value)
    return source, managed


def test_setup_writes_only_to_the_resolved_locations(external):
    source, managed = external
    locations = workspace_locations(source)
    assert locations.state_db == managed / "state.db"
    assert locations.state_db.is_file()
    assert not (source / ".agent-dev").exists()
    marker = metadata(managed)
    assert marker is not None
    assert marker["workspace_id"] == locations.workspace_id
    assert marker["source_root"] == str(source)
    assert (managed / "config/runtime.json").is_file()
    assert environment_file(source) == locations.environment_file
    assert read_environment(source) == {"APP_TOKEN": "external-private-value"}
    assert locations.environment_file.stat().st_mode & 0o077 == 0
    assert locations.environment_file.parent.stat().st_mode & 0o077 == 0
    assert "external-private-value" not in (managed / "state.db").read_bytes().decode(
        "utf-8", "ignore"
    )
    settings = ProjectSetup(source).read()
    assert settings["revision"] == 1
    assert settings["root"] == str(source)
    assert settings["configuration_root"] == str(managed)


def test_runtime_queries_and_integrations_follow_locations(external, monkeypatch):
    source, managed = external
    # Configuring the HTTP connection resolves its host; keep the test off live DNS.
    monkeypatch.setattr("agent_dev.adapters.integrations.remote_address", lambda *a, **kw: None)
    locations = workspace_locations(source)
    runtime = WorkflowRuntime(source)
    assert runtime.store.db.path == locations.state_db
    assert runtime.project_root == locations.source_root
    assert runtime.artifacts.root == locations.output_root
    assert runtime.root == managed
    assert TaskQueries(source).db is not None
    assert TaskQueries(source).db.path == locations.state_db
    store = IntegrationStore(source)
    assert store.path == locations.state_db
    app = Integrations(source)
    app.configure({"id": "docs", "provider": "context7", "scopes": ["react"]})
    assert [row["id"] for row in app.list()] == ["docs"]
    assert IntegrationOAuth(source).path("docs").parent == locations.oauth_dir
    assert DesignReferences(source).directory == locations.cache_root / "design-references"
    console = AgentConsole(source)
    assert console.db_path == locations.state_db
    assert console.uploads == locations.runtime_root / "web-inputs"
    studio = StudioStore(managed)
    assert studio.path == locations.studio_db
    assert ui_snapshot(managed)["status"] is not None
    report = diagnose(source)
    assert (
        next(c for c in report["checks"] if c["check"] == "database_integrity")["status"] == "pass"
    )
    assert not (source / ".agent-dev").exists()


def test_role_override_comes_from_the_config_folder(external):
    source, managed = external
    locations = workspace_locations(source)
    locations.role_dir.mkdir()
    (locations.role_dir / "qa.md").write_text("# Managed QA\n", encoding="utf-8")
    text, origin = role_override(managed, "qa")
    assert text == "# Managed QA\n"
    assert origin["path"] == "roles/qa.md"
    assert origin["location"] == "config"
    assert origin["workspace_id"] == locations.workspace_id
    # The source repository's own .agent-dev folder is never an instruction source here.
    (source / ".agent-dev/roles").mkdir(parents=True)
    (source / ".agent-dev/roles/developer.md").write_text("# Repository developer\n", encoding="utf-8")
    assert role_override(managed, "developer") is None


def test_legacy_role_override_keeps_its_recorded_path(tmp_path):
    root = tmp_path / "project"
    initialize(root, name="Example", project_id="EXAMPLE", paths=["src"])
    (root / ".agent-dev/roles").mkdir()
    (root / ".agent-dev/roles/qa.md").write_text("# Local QA\n", encoding="utf-8")
    _, origin = role_override(root / ".agent-dev", "qa")
    assert origin == {
        "source": "override",
        "path": ".agent-dev/roles/qa.md",
        "digest": origin["digest"],
    }


class Runner:
    def execute(self, request):
        if request.phase_id == "developer":
            binding = json.loads(request.context)["repositories"]["source"]
            (Path(binding["worktree"]) / "src/source.txt").write_text("implemented", encoding="utf-8")
        return PhaseResult('```json\n{"handoff_status":"pass"}\n```')


def approved(source, managed):
    runtime = WorkflowRuntime(source)
    profile = json.loads((managed / "config/profile.json").read_text(encoding="utf-8"))
    # The preserved legacy-linear three-phase contract, as in test_reusable_workspace.
    profile.pop("workflow", None)
    profile["role_context"] = {
        key: value
        for key, value in profile["role_context"].items()
        if key in {"planner", "developer", "qa"}
    }
    profile["runners"][0]["authenticated"] = True
    version = runtime.store.db.rows("SELECT version FROM projects WHERE id='EXAMPLE'")[0]["version"]
    runtime.store.revise_profile("EXAMPLE", profile, version)
    runtime.store.design(
        "EXAMPLE",
        "D",
        "## Scope\nChange source\n## Acceptance\nVerify diff\n## Constraints\nOnly src",
    )
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan(
        "EXAMPLE", "PLAN", "D", 1, [{"id": "T", "title": "Example task", "system_name": "Example"}]
    )
    runtime.store.approve("plan", "PLAN", 1, "user")
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    path = managed / "config/runtime.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["orchestrator_v2"]["execute"] = True
    path.write_text(json.dumps(value), encoding="utf-8")
    return runtime


def test_external_task_runs_in_source_worktree_and_keeps_evidence(external):
    source, managed = external
    locations = workspace_locations(source)
    runtime = approved(source, managed)
    resolved = runtime.store.snapshot("T")["resolved"]
    worktree = Path(resolved["repositories"]["source"]["worktree"])
    assert worktree.is_relative_to(locations.worktree_root)
    assert runtime.validate_snapshot_paths("T") == resolved
    assert runtime.execute("T", "run", runner=Runner())["state"] == "verifying"
    assert runtime.execute("T", "verify", verify=True)["passed"]
    for row in runtime.store.db.rows("SELECT path FROM artifacts WHERE task_id='T'"):
        assert (locations.output_root / row["path"]).is_file()
    report = TaskQueries(source).explain("T")
    assert report["approvals"] == {"design": True, "plan": True, "snapshot": True}
    assert (source / "src/source.txt").read_text(encoding="utf-8") == "baseline"
    assert not (source / ".agent-dev/state.db").exists()
