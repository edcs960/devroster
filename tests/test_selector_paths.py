"""Frozen pytest cwd/selector path contract, with actual approved executions."""

import json
from typing import Any, cast

import pytest
from test_project_journey import workspace as journey_workspace

from agent_dev.adapters import studio_handoff, traceability
from agent_dev.adapters.artifacts import FilesystemArtifacts
from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
from agent_dev.adapters.verification import VerificationAdapter
from agent_dev.domain.errors import DomainError


@pytest.fixture
def workspace(tmp_path):
    return cast(Any, journey_workspace).__wrapped__(tmp_path)


def approved(workspace, cwd, target, *, execute=True, ambiguous=False):
    root, repo, source, _old = workspace
    profile = json.loads(
        repo.db.rows("SELECT content_json FROM project_profile_versions")[0]["content_json"]
    )
    gate = profile["verification"]["commands"][0]
    gate["cwd"] = cwd
    gate["command"][-1] = target
    if ambiguous:
        (source / "other").mkdir()
        (source / "other/test_acceptance.py").write_text(
            "def test_acceptance():\n    assert True\n"
        )
        profile["repositories"]["backend"]["allowed_paths"] = ["tests", "other"]
        profile["verification"]["commands"].append(dict(gate, name="other", cwd="other"))
    repo.revise_profile("P1", profile, 0)
    first = studio_handoff.preview(
        repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"}
    )
    tasks = first["selection"]["tasks"]
    tasks[0]["verification_mapping"] = [
        {
            "requirement_id": "REQ-001",
            "criteria_ids": ["REQ-001:AC-01"],
            "gate": "acceptance",
            "target": target,
        }
    ]
    if ambiguous:
        tasks[0]["verification_gates"] = ["acceptance", "other"]
        tasks[0]["verification_mapping"].append(
            dict(tasks[0]["verification_mapping"][0], gate="other")
        )
    first = studio_handoff.preview(repo.db, root, dict(first["selection"], tasks=tasks))
    imported = studio_handoff.commit(
        repo.db,
        root,
        dict(first["selection"], preview_digest=first["preview_digest"], request_id="PATH-IMPORT"),
    )
    repo.approve("design", imported["design_id"], 1, "user")
    repo.approve("plan", imported["plan_id"], 1, "user")
    task = tasks[0]["id"]
    run = None
    if execute:
        profile["repositories"]["backend"]["worktree"] = str(source)
        profile["design"] = repo.db.rows("SELECT content FROM design_documents")[0]["content"]
        repo.create_snapshot(task, profile)
        with repo.db.transaction() as conn:
            conn.execute("UPDATE tasks SET state='verifying' WHERE id=?", (task,))
        with ExecutionSafety(repo).lease("task:" + task) as lease:
            result = VerificationAdapter(repo, FilesystemArtifacts(repo, root / "output")).verify(
                task, lease
            )
        assert result["passed"]
        run = result["run_id"]
    return {
        "import_id": imported["import_id"],
        "requirement_id": "REQ-001",
        "kind": "test",
        "target_ref": target,
        "task_id": task,
        "module_id": "backend",
        "criteria_ids": ["REQ-001:AC-01"],
        "verification_run_id": run,
        "request_id": "PATH-LINK",
        "expected_version": 0,
    }


@pytest.mark.parametrize(
    "cwd,target",
    [
        (".", "./tests/test_acceptance.py::test_acceptance"),
        ("tests", "test_acceptance.py::test_acceptance"),
    ],
)
def test_frozen_cwd_relative_actual_pass_and_current_file_stale(workspace, cwd, target):
    params = approved(workspace, cwd, target)
    _root, repo, source, _old = workspace
    link = traceability.create_link(repo.db, params)
    assert link["state"] == "verified"
    stored = repo.db.rows("SELECT * FROM requirement_links WHERE id=?", (link["id"],))[0]
    assert stored["target_ref"] == target
    (source / "tests/test_acceptance.py").write_text("def test_acceptance():\n    assert False\n")
    assert traceability.detail(repo.db, link["id"])["link"]["state"] == "stale"
    assert (
        repo.db.rows("SELECT state FROM requirement_links WHERE id=?", (link["id"],))[0]["state"]
        == "verified"
    )


@pytest.mark.parametrize(
    "cwd,target",
    [
        (".", "./tests/test_acceptance.py::test_acceptance"),
        ("tests", "test_acceptance.py::test_acceptance"),
    ],
)
def test_manual_registered_selector_uses_gate_cwd_for_file_digest(workspace, cwd, target):
    params = approved(workspace, cwd, target)
    params["verification_run_id"] = None
    _root, repo, source, _old = workspace
    link = traceability.create_link(repo.db, params)
    assert link["state"] == "mapped"
    (source / "tests/test_acceptance.py").write_text("def test_acceptance():\n    assert False\n")
    assert traceability.detail(repo.db, link["id"])["link"]["state"] == "stale"
    assert (
        traceability.query(repo.db, {"import_id": params["import_id"], "state": "stale"})["links"][
            0
        ]["id"]
        == link["id"]
    )
    assert (
        repo.db.rows("SELECT state FROM requirement_links WHERE id=?", (link["id"],))[0]["state"]
        == "mapped"
    )


@pytest.mark.parametrize(
    "cwd,target",
    [
        (".", "/tests/test_acceptance.py::test_acceptance"),
        (".", "../tests/test_acceptance.py::test_acceptance"),
        ("../outside", "test_acceptance.py::test_acceptance"),
        (".", "outside/test_acceptance.py::test_acceptance"),
        ("tests", ".env.py::test_acceptance"),
        ("tests", "credentials/test_acceptance.py::test_acceptance"),
        ("alias", "test_acceptance.py::test_acceptance"),
        ("tests", "alias.py::test_acceptance"),
    ],
)
def test_registered_selector_keeps_private_nofollow_and_allowed_boundaries(workspace, cwd, target):
    _root, repo, source, _old = workspace
    (source / "alias").symlink_to(source / "tests", target_is_directory=True)
    (source / "tests/alias.py").symlink_to(source / "tests/test_acceptance.py")
    params = approved(workspace, cwd, target, execute=False)
    with pytest.raises(DomainError):
        traceability.create_link(repo.db, params)


def test_ambiguous_same_selector_different_gate_cwds_is_rejected(workspace):
    params = approved(workspace, "tests", "test_acceptance.py::test_acceptance", ambiguous=True)
    _root, repo, _source, _old = workspace
    with pytest.raises(DomainError, match="모호"):
        traceability.create_link(repo.db, params)


def test_file_link_still_uses_module_root_not_test_cwd(workspace):
    params = approved(workspace, "tests", "test_acceptance.py::test_acceptance")
    _root, repo, _source, _old = workspace
    params.update(
        kind="file",
        target_ref="tests/test_acceptance.py",
        verification_run_id=None,
        criteria_ids=[],
    )
    assert traceability.create_link(repo.db, params)["state"] == "mapped"
    with pytest.raises(DomainError):
        traceability.create_link(
            repo.db, dict(params, target_ref="test_acceptance.py", request_id="ROOT-ONLY")
        )
