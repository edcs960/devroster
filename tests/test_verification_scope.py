"""With verification scope "changed", gates of untouched repositories are skipped."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.adapters.worktrees import changed_repositories
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def repository(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.test")
    (path / ".gitignore").write_text("ignored/\n")
    (path / "file.txt").write_text("base")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


def gate(name, repo, marker):
    # The marker lives outside every worktree, so writing it never changes source evidence.
    script = f"open({str(marker)!r}, 'a').write({name!r} + chr(10))"
    return {"name": name, "repository": repo, "command": [sys.executable, "-c", script]}


def prepared(tmp_path, scope):
    source, other = repository(tmp_path / "source"), repository(tmp_path / "other")
    marker = tmp_path / "ran.txt"
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(
        json.dumps({"orchestrator_v2": {"execute": True, "publish": False}})
    )
    runtime = WorkflowRuntime(root)
    verification = {
        "commands": [gate("source_unit", "source", marker), gate("other_unit", "other", marker)],
        "required_gates": ["source_unit", "other_unit"],
    }
    if scope:
        verification["scope"] = scope
    runtime.store.register_project(
        "P",
        "Project",
        {
            "repositories": {
                "source": {"path": str(source), "allowed_paths": ["file.txt"]},
                "other": {"path": str(other), "allowed_paths": ["file.txt"]},
            },
            "phases": [{"id": "developer", "capabilities": ["code.write"]}],
            "verification": verification,
        },
    )
    runtime.store.design("P", "D", "## Scope\nS\n## Acceptance\nA\n## Constraints\nC")
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan("P", "PLAN", "D", 1, [{"id": "T", "title": "Implement"}])
    runtime.store.approve("plan", "PLAN", 1, "user")
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    return runtime, marker


class SourceOnly:
    def execute(self, request):
        context = json.loads(request.context)
        (Path(context["repositories"]["source"]["worktree"]) / "file.txt").write_text("changed")
        return PhaseResult('```json\n{"handoff_status":"pass"}\n```')


def gate_rows(runtime):
    rows = runtime.store.db.rows(
        "SELECT gate,passed,exit_code,evidence FROM verification_results ORDER BY gate"
    )
    return {r["gate"]: (bool(r["passed"]), r["exit_code"], json.loads(r["evidence"])["text"]) for r in rows}


def test_changed_scope_skips_untouched_repository_gates(tmp_path):
    runtime, marker = prepared(tmp_path, "changed")
    assert runtime.execute("T", "run", runner=SourceOnly())["state"] == "verifying"
    assert runtime.execute("T", "verify", verify=True)["passed"]
    assert marker.read_text().split() == ["source_unit"]
    rows = gate_rows(runtime)
    assert rows["source_unit"][:2] == (True, 0)
    assert rows["other_unit"][:2] == (True, None)
    assert rows["other_unit"][2].startswith("Skipped: repository 'other' has no changes")


def test_default_scope_runs_every_gate(tmp_path):
    runtime, marker = prepared(tmp_path, None)
    assert runtime.execute("T", "run", runner=SourceOnly())["state"] == "verifying"
    assert runtime.execute("T", "verify", verify=True)["passed"]
    assert sorted(marker.read_text().split()) == ["other_unit", "source_unit"]


def test_changed_repositories_counts_commits_edits_and_untracked_files(tmp_path):
    repo = repository(tmp_path / "repo")
    base = git(repo, "rev-parse", "HEAD")
    bindings = {"repo": {"worktree": str(repo), "base_commit": base}}
    assert changed_repositories(bindings) == set()
    (repo / "ignored").mkdir()
    (repo / "ignored/cache.bin").write_text("x")
    assert changed_repositories(bindings) == set()
    (repo / "new.txt").write_text("x")
    assert changed_repositories(bindings) == {"repo"}
    git(repo, "add", "new.txt")
    git(repo, "commit", "-qm", "add")
    assert changed_repositories(bindings) == {"repo"}
    # Without a known base commit the repository is never skipped.
    assert changed_repositories({"repo": {"worktree": str(repo)}}) == {"repo"}


def test_scope_round_trips_through_settings(tmp_path):
    root = tmp_path / "project"
    value = ProjectSetup(root).read()
    assert value["verification_scope"] == "all"
    value["create_directories"] = True
    value["verification_scope"] = "changed"
    ProjectSetup(root).save(value)
    _, _, profile = ProjectSetup(root).current_profile()
    assert profile["verification"]["scope"] == "changed"
    loaded = ProjectSetup(root).read()
    assert loaded["verification_scope"] == "changed"
    loaded["verification_scope"] = "all"
    ProjectSetup(root).save(loaded)
    _, _, profile = ProjectSetup(root).current_profile()
    assert "scope" not in profile["verification"]


def test_unknown_scope_is_rejected(tmp_path):
    root = tmp_path / "project"
    value = ProjectSetup(root).read()
    value["create_directories"] = True
    value["verification_scope"] = "some"
    with pytest.raises(DomainError):
        ProjectSetup(root).save(value)


def test_workflow_verification_node_uses_the_same_scope(tmp_path):
    from agent_dev.adapters.phase_catalog import SnapshotPhaseCatalog

    runtime, marker = prepared(tmp_path, "changed")
    assert runtime.execute("T", "run", runner=SourceOnly())["state"] == "verifying"
    with runtime.safety.lease("task:T") as lease:
        result = SnapshotPhaseCatalog(runtime.store, runtime.root).verify_node("T", [], lease)
    assert result["passed"] is True
    gates = {g["gate"]: g for g in result["gates"]}
    assert gates["source_unit"]["exit_code"] == 0
    assert gates["other_unit"]["exit_code"] is None
    assert gates["other_unit"]["evidence"].startswith("Skipped: repository 'other'")
    assert marker.read_text().split() == ["source_unit"]
