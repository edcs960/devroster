"""Behavioral regression cases for Task3 SPEC-01..04."""

import json
import sqlite3
import sys
from typing import Any, cast

import pytest
from test_project_journey import actual_run, prepared
from test_project_journey import workspace as journey_workspace

from agent_dev.adapters import studio_handoff, traceability
from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError


@pytest.fixture
def workspace(tmp_path):
    return cast(Any, journey_workspace).__wrapped__(tmp_path)


@pytest.mark.parametrize("case", ["collect", "filter", "deselect", "skip", "xfail", "unsupported"])
def test_exit_zero_without_target_pass_is_not_acceptance_proof(workspace, case):
    _root, repo, source, target = workspace
    body = "def test_acceptance():\n    assert False\n\ndef test_other():\n    assert True\n"
    if case in {"skip", "xfail"}:
        body = "import pytest\n@pytest.mark." + case + "\n" + body
    (source / "tests/test_acceptance.py").write_text(body, encoding="utf-8")
    command = [sys.executable, "-m", "pytest", target]
    other = "tests/test_acceptance.py::test_other"
    command += {
        "collect": ["--collect-only"],
        "filter": [other, "-k", "test_other"],
        "deselect": [other, "--deselect=" + target],
    }.get(case, [])
    if case == "unsupported":
        command = [sys.executable, "-c", "pass", target]
    # The fixture owns these immutable profile rows; establish the registered policy before use.
    with repo.db.transaction() as conn:
        profile = json.loads(
            conn.execute("SELECT content_json FROM project_profile_versions").fetchone()[0]
        )
        profile["verification"]["commands"][0]["command"] = command
        conn.execute("DELETE FROM project_profile_versions")
        conn.execute(
            "INSERT INTO project_profile_versions VALUES(?,?,?,?)",
            ("P1", 1, digest(profile), json.dumps(profile)),
        )
    first, imported = prepared(workspace)
    run, task = actual_run(workspace, first)
    link = traceability.create_link(
        repo.db,
        {
            "import_id": imported["import_id"],
            "requirement_id": "REQ-001",
            "kind": "test",
            "target_ref": target,
            "task_id": task,
            "module_id": "backend",
            "criteria_ids": ["REQ-001:AC-01"],
            "verification_run_id": run,
            "request_id": "NOT-EXECUTED",
            "expected_version": 0,
        },
    )
    assert link["state"] == "mapped"
    assert traceability.coverage(repo.db, imported["import_id"])["verified"] == 0


def test_manual_test_file_change_is_derived_stale(workspace):
    first, imported = prepared(workspace)
    _root, repo, source, target = workspace
    link = traceability.create_link(
        repo.db,
        {
            "import_id": imported["import_id"],
            "requirement_id": "REQ-001",
            "kind": "test",
            "target_ref": target,
            "task_id": first["selection"]["tasks"][0]["id"],
            "module_id": "backend",
            "criteria_ids": [],
            "request_id": "MANUAL",
            "expected_version": 0,
        },
    )
    (source / "tests/test_acceptance.py").write_text("def test_acceptance():\n    assert False\n", encoding="utf-8")
    assert traceability.detail(repo.db, link["id"])["link"]["state"] == "stale"
    page = traceability.query(repo.db, {"import_id": imported["import_id"], "state": "stale"})
    assert link["id"] in {row["id"] for row in page["links"]}
    assert (
        repo.db.rows("SELECT state FROM requirement_links WHERE id=?", (link["id"],))[0]["state"]
        == "mapped"
    )


def test_cursor_is_validated_and_bound_to_filters(workspace):
    _first, imported = prepared(workspace)
    _root, repo, _source, _target = workspace
    scope = {"import_id": imported["import_id"], "limit": 1}
    cursor = traceability.query(repo.db, scope)["next_cursor"]
    assert cursor
    for invalid in [
        "zzzz-not-a-cursor",
        "x" * 4097,
        cursor[:-1] + ("a" if cursor[-1] != "a" else "b"),
    ]:
        with pytest.raises(DomainError):
            traceability.query(repo.db, dict(scope, cursor=invalid))
    with pytest.raises(DomainError):
        traceability.query(repo.db, dict(scope, cursor=cursor, state="verified"))
    with pytest.raises(DomainError):
        traceability.query(repo.db, dict(scope, cursor=cursor, requirement_id="REQ-002"))


def test_requirements_cursor_reaches_205_without_duplicates(workspace):
    root, repo, _source, _target = workspace
    with sqlite3.connect(root / ".agent-dev/studio/studio.db") as conn:
        payload = json.loads(conn.execute("SELECT payload FROM specs").fetchone()[0])
        original = payload["requirements"][0]
        payload["requirements"] = [
            dict(original, id=f"REQ-{i:03d}", linked_screens=[]) for i in range(1, 206)
        ]
        spec_digest = digest(payload)
        conn.execute("UPDATE specs SET payload=?,digest=?", (json.dumps(payload), spec_digest))
        mock_digest = conn.execute("SELECT digest FROM mocks").fetchone()[0]
        conn.execute(
            "UPDATE approvals SET digest=?",
            (digest({"mock": mock_digest, "spec": spec_digest, "screen": "SCR-001"}),),
        )
    preview = studio_handoff.preview(
        repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"}
    )
    imported = studio_handoff.commit(
        repo.db,
        root,
        dict(preview["selection"], preview_digest=preview["preview_digest"], request_id="MANY"),
    )
    repo.approve("design", imported["design_id"], 1, "user")
    repo.approve("plan", imported["plan_id"], 1, "user")
    scope = {"import_id": imported["import_id"]}
    first = traceability.query(repo.db, scope)
    second = traceability.query(
        repo.db, dict(scope, requirements_cursor=first["requirements_next_cursor"])
    )
    ids = [r["requirement_id"] for r in first["requirements"] + second["requirements"]]
    assert len(ids) == len(set(ids)) == 205
    assert second["requirements_next_cursor"] is None
    selected = traceability.query(repo.db, dict(scope, requirement_id="REQ-205"))
    assert any("REQ-205" in t["requirement_ids"] for t in selected["tasks"])


def test_malformed_host_observations_fail_closed(tmp_path):
    from agent_dev.adapters.pytest_attestation import _read

    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "nonce": "N",
                "exit_code": 0,
                "selected": [{"nodeid": "test_x.py::test_x", "path": str(tmp_path / "test_x.py")}],
                "excluded": [],
                "reports": ["bad"],
            }
        ), encoding="utf-8"
    )
    assert _read(path, "N", 0, tmp_path, ["python"]) is None


@pytest.mark.parametrize(
    "case,expected",
    [
        ("file", True),
        ("file_filtered", False),
        ("file_skip", False),
        ("parameterized", True),
        ("parameter_filtered", False),
        ("parameter_exact", True),
        ("parameter_skip", False),
        ("xpass", False),
        ("teardown_failed", False),
    ],
)
def test_host_pytest_actual_selector_scope(tmp_path, case, expected, monkeypatch):
    import subprocess

    from agent_dev.adapters.pytest_attestation import observe, selector_passed

    target = "test_scope.py"
    body = "def test_first():\n    assert True\ndef test_other():\n    assert True\n"
    flags = []
    if case.startswith("parameter"):
        body = 'import pytest\n@pytest.mark.parametrize("n", [1,2])\ndef test_first(n):\n    assert n > 0\n'
        target += "::test_first"
        if case == "parameter_filtered":
            flags = ["-k", "1"]
        if case == "parameter_exact":
            target += "[1]"
        if case == "parameter_skip":
            body = body.replace("assert n > 0", "pytest.skip() if n == 2 else None")
    if case == "file_filtered":
        flags = ["-k", "test_other"]
    if case == "file_skip":
        body = "import pytest\n@pytest.mark.skip\n" + body
    if case == "xpass":
        body = "import pytest\n@pytest.mark.xfail(strict=False)\n" + body
    if case == "teardown_failed":
        body = (
            "import pytest\n@pytest.fixture(autouse=True)\ndef fixture():\n    yield\n    assert False\n"
            + body
        )
    (tmp_path / "test_scope.py").write_text(body, encoding="utf-8")
    command = [sys.executable, "-m", "pytest", target, *flags]
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    with observe(command, None, tmp_path, tmp_path) as (env, read):
        assert env is not None
        directory = __import__("pathlib").Path(env["PYTHONPATH"].split(__import__("os").pathsep)[0])
        process = subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
        proof = read(process.returncode)
        assert selector_passed(proof, target, command) is expected
    assert directory is not None and not directory.exists()
    assert not list(tmp_path.glob("*observations*"))


def test_observation_files_missing_nonce_truncated_symlink_and_timeout(tmp_path):
    import subprocess
    from pathlib import Path

    from agent_dev.adapters.pytest_attestation import _read, observe

    command = [sys.executable, "-m", "pytest", "test_timeout.py"]
    path = tmp_path / "raw.json"
    assert _read(path, "N", 0, tmp_path, command) is None
    for raw in ["{", json.dumps({"nonce": "WRONG", "exit_code": 0}), "x" * 1_000_001]:
        path.write_text(raw, encoding="utf-8")
        assert _read(path, "N", 0, tmp_path, command) is None
    link = tmp_path / "symlink.json"
    link.symlink_to(path)
    assert _read(link, "N", 0, tmp_path, command) is None
    (tmp_path / "test_timeout.py").write_text(
        "import time\ndef test_timeout():\n    time.sleep(10)\n", encoding="utf-8"
    )
    directory = None
    with (
        pytest.raises(subprocess.TimeoutExpired),
        observe(command, None, tmp_path, tmp_path) as (env, _read_result),
    ):
        assert env is not None
        directory = Path(env["PYTHONPATH"].split(__import__("os").pathsep)[0])
        subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, timeout=0.2, check=False
        )
    assert directory is not None and not directory.exists()


def test_cursor_rejects_changed_revision_and_cross_scope(workspace):
    _first, imported = prepared(workspace)
    _root, repo, _source, _target = workspace
    scope = {"import_id": imported["import_id"], "limit": 1}
    token = traceability.query(repo.db, scope)["next_cursor"]
    with pytest.raises(DomainError):
        traceability.query(repo.db, dict(scope, project_id="P1", cursor=token))
    with repo.db.transaction() as conn:
        conn.execute("UPDATE requirement_links SET version=version+1")
    with pytest.raises(DomainError) as exc:
        traceability.query(repo.db, dict(scope, cursor=token))
    assert exc.value.code.value == "STALE_VERSION"


def test_task_and_run_choices_have_bounded_continuations(workspace):
    first, imported = prepared(workspace)
    _root, repo, _source, _target = workspace
    template = first["selection"]["tasks"][0]
    with repo.db.transaction() as conn:
        for i in range(205):
            task = "CHOICE-" + str(i).zfill(3)
            conn.execute(
                "INSERT INTO tasks(id,project_id,plan_id,title,priority,state,definition_json) VALUES(?,'P1',?,?,'medium','planned',?)",
                (task, imported["plan_id"], task, json.dumps(template)),
            )
            conn.execute(
                "INSERT INTO requirement_links(id,import_id,requirement_id,kind,target_ref,task_id,module_id,evidence_digest,state) VALUES(?,?,'REQ-002','task',?,?,'backend','','mapped')",
                ("CHOICE-LINK-" + str(i), imported["import_id"], task, task),
            )
        for i in range(105):
            conn.execute(
                "INSERT INTO verification_runs VALUES(?,?,'passed','old-unattested',?)",
                ("RUN-" + str(i), "CHOICE-204", i),
            )
    scope = {"import_id": imported["import_id"], "requirement_id": "REQ-002", "limit": 1}
    one = traceability.query(repo.db, scope)
    two = traceability.query(repo.db, dict(scope, tasks_cursor=one["tasks_next_cursor"]))
    assert len(one["tasks"]) == 200 and two["tasks_next_cursor"] is None
    assert len({t["id"] for t in one["tasks"] + two["tasks"]}) == 206
    scope["task_id"] = "CHOICE-204"
    one = traceability.query(repo.db, scope)
    two = traceability.query(repo.db, dict(scope, runs_cursor=one["runs_next_cursor"]))
    assert len(one["runs"]) == 100 and two["runs_next_cursor"] is None
    assert len({r["id"] for r in one["runs"] + two["runs"]}) == 105


def test_legacy_report_without_selector_attestation_stays_mapped(workspace, monkeypatch):
    first, imported = prepared(workspace)
    run, task = actual_run(workspace, first)
    _root, repo, _source, target = workspace
    original = traceability._artifact_report

    def legacy(*args):
        report = original(*args)
        assert report is not None
        for gate in report["gates"]:
            gate.pop("test_results", None)
        return report

    monkeypatch.setattr(traceability, "_artifact_report", legacy)
    link = traceability.create_link(
        repo.db,
        {
            "import_id": imported["import_id"],
            "requirement_id": "REQ-001",
            "kind": "test",
            "target_ref": target,
            "task_id": task,
            "module_id": "backend",
            "criteria_ids": ["REQ-001:AC-01"],
            "verification_run_id": run,
            "request_id": "LEGACY",
            "expected_version": 0,
        },
    )
    assert link["state"] == "mapped"
