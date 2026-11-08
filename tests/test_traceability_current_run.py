"""Current verification supersedes historical success without rewriting evidence."""

import json
import subprocess
from typing import Any, cast

import pytest
from test_project_journey import prepared
from test_project_journey import workspace as journey_workspace
from test_workflow_sqlite import PASS, coordinator

from agent_dev.adapters import traceability
from agent_dev.adapters.artifacts import FilesystemArtifacts
from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
from agent_dev.adapters.verification import VerificationAdapter
from agent_dev.adapters.worktrees import evidence_digest
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext


@pytest.fixture
def workspace(tmp_path):
    return cast(Any, journey_workspace).__wrapped__(tmp_path)


@pytest.mark.parametrize("same_timestamp", [False, True])
def test_latest_actual_failure_and_repass_require_explicit_latest_link(
    workspace, monkeypatch, same_timestamp
):
    root, repo, source, target = workspace
    (source / "tests/test_acceptance.py").write_text(
        'import os\ndef test_acceptance():\n    assert os.environ.get("TASK3_FORCE_FAILURE") != "1"\n', encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "conditional gate",
        ],
        check=True,
    )
    profile = json.loads(
        repo.db.rows("SELECT content_json FROM project_profile_versions")[0]["content_json"]
    )
    profile["phases"] = [{"id": "developer", "capabilities": ["code.write"]}]
    repo.revise_profile("P1", profile, 0)
    first, imported = prepared(workspace)
    task_id = first["selection"]["tasks"][0]["id"]
    profile["repositories"]["backend"]["worktree"] = str(source)
    profile["design"] = repo.db.rows("SELECT content FROM design_documents")[0]["content"]
    repo.create_snapshot(task_id, profile)
    if same_timestamp:
        repo.clock = lambda: 1000.0
    flow, runner = coordinator(repo, root, [PASS])
    adapter = VerificationAdapter(repo, FilesystemArtifacts(repo, root / "output"))
    before = evidence_digest(profile["repositories"])

    def verify_again(block=False):
        with ExecutionSafety(repo).lease("task:" + task_id) as lease:
            if block:
                repo.transition(
                    task_id,
                    TaskStatus.BLOCKED,
                    TransitionContext(expected_version=repo.task(task_id).version),
                    lease,
                )
            assert flow.execute(task_id, lease)["state"] == "verifying"
            return adapter.verify(task_id, lease)

    initial = verify_again()
    assert initial["passed"]
    params = {
        "import_id": imported["import_id"],
        "requirement_id": "REQ-001",
        "kind": "test",
        "target_ref": target,
        "task_id": task_id,
        "module_id": "backend",
        "criteria_ids": ["REQ-001:AC-01"],
        "verification_run_id": initial["run_id"],
        "request_id": "INITIAL",
        "expected_version": 0,
    }
    link = traceability.create_link(repo.db, params)
    assert link["state"] == "verified"
    original = dict(repo.db.rows("SELECT * FROM requirement_links WHERE id=?", (link["id"],))[0])
    monkeypatch.setenv("TASK3_FORCE_FAILURE", "1")
    failure = verify_again(block=True)
    assert not failure["passed"] and repo.task(task_id).state == TaskStatus.BLOCKED
    assert evidence_digest(profile["repositories"]) == before
    assert runner.calls == 1
    assert traceability.detail(repo.db, link["id"])["link"]["state"] == "stale"
    assert traceability.coverage(repo.db, imported["import_id"])["verified"] == 0
    page = traceability.query(repo.db, {"import_id": imported["import_id"], "state": "stale"})
    assert link["id"] in {row["id"] for row in page["links"]}
    assert (
        dict(repo.db.rows("SELECT * FROM requirement_links WHERE id=?", (link["id"],))[0])
        == original
    )
    relink = traceability.create_link(
        repo.db, dict(params, request_id="OLD-RELINK", expected_version=link["version"])
    )
    assert relink["state"] == "mapped"
    monkeypatch.delenv("TASK3_FORCE_FAILURE")
    success = verify_again()
    assert success["passed"]
    assert traceability.detail(repo.db, link["id"])["link"]["state"] != "verified"
    assert traceability.coverage(repo.db, imported["import_id"])["verified"] == 0
    current = traceability.create_link(
        repo.db,
        dict(
            params,
            request_id="LATEST-RELINK",
            expected_version=relink["version"],
            verification_run_id=success["run_id"],
        ),
    )
    assert current["state"] == "verified"
    assert traceability.coverage(repo.db, imported["import_id"])["verified"] == 1
    assert (
        repo.db.rows("SELECT state FROM verification_runs WHERE id=?", (initial["run_id"],))[0][
            "state"
        ]
        == "passed"
    )
