"""Home/HTTP boundary and actual requirement evidence proof integration."""

import http.client
import json
import subprocess
import sys
import threading
from typing import Any

import pytest
from test_studio_handoff import _setup_studio_project

from agent_dev.adapters import studio_handoff, traceability
from agent_dev.adapters.artifacts import FilesystemArtifacts
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.adapters.verification import VerificationAdapter
from agent_dev.domain.errors import DomainError
from agent_dev.ui.journey_server import JourneyWeb
from agent_dev.ui.local_web import make_server


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = tmp_path / "repo"
    source.mkdir()
    (source / "tests").mkdir()
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (source / "tests/test_acceptance.py").write_text(
        "def test_acceptance():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(source)], check=True)
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
            "base",
        ],
        check=True,
    )
    db = Database(root / ".agent-dev/state.db")
    repo = WorkflowRepository(db)
    target = "tests/test_acceptance.py::test_acceptance"
    repo.register_project(
        "P1",
        "Project",
        {
            "repositories": {"backend": {"path": str(source), "allowed_paths": ["tests"]}},
            "verification": {
                "commands": [
                    {
                        "name": "acceptance",
                        "repository": "backend",
                        "command": [sys.executable, "-m", "pytest", target],
                    }
                ]
            },
        },
    )
    _setup_studio_project(StudioStore(root), "STUDIO-001")
    return root, repo, source, target


def prepared(workspace, mapped=True):
    root, repo, _source, target = workspace
    first = studio_handoff.preview(
        repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"}
    )
    tasks = first["selection"]["tasks"]
    if mapped:
        tasks[0]["verification_mapping"] = [
            {
                "requirement_id": "REQ-001",
                "criteria_ids": ["REQ-001:AC-01"],
                "gate": "acceptance",
                "target": target,
            }
        ]
    first = studio_handoff.preview(repo.db, root, dict(first["selection"], tasks=tasks))
    result = studio_handoff.commit(
        repo.db,
        root,
        dict(first["selection"], preview_digest=first["preview_digest"], request_id="IMPORT-1"),
    )
    repo.approve("design", result["design_id"], 1, "user")
    repo.approve("plan", result["plan_id"], 1, "user")
    return first, result


def actual_run(workspace, first):
    root, repo, source, _target = workspace
    task_id = first["selection"]["tasks"][0]["id"]
    design = repo.db.rows("SELECT content FROM design_documents")[0]["content"]
    profile = json.loads(
        repo.db.rows("SELECT content_json FROM project_profile_versions")[0]["content_json"]
    )
    profile["repositories"]["backend"]["worktree"] = str(source)
    repo.create_snapshot(task_id, dict(profile, design=design))
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET state='verifying' WHERE id=?", (task_id,))
    with ExecutionSafety(repo).lease("task:" + task_id) as lease:
        result = VerificationAdapter(repo, FilesystemArtifacts(repo, root / "output")).verify(
            task_id, lease
        )
    assert result["passed"]
    return result["run_id"], task_id


def test_actual_registered_target_proof_and_stale_without_history_mutation(workspace):
    first, result = prepared(workspace)
    run_id, task_id = actual_run(workspace, first)
    _root, repo, source, target = workspace
    params = {
        "import_id": result["import_id"],
        "requirement_id": "REQ-001",
        "kind": "test",
        "target_ref": target,
        "task_id": task_id,
        "module_id": "backend",
        "criteria_ids": ["REQ-001:AC-01"],
        "verification_run_id": run_id,
        "request_id": "LINK-1",
        "expected_version": 0,
    }
    link = traceability.create_link(repo.db, params)
    assert link["state"] == "verified"
    assert traceability.create_link(repo.db, params) == link
    queried = traceability.query(repo.db, {"import_id": result["import_id"]})
    assert queried["coverage"] == {
        "verified": 1,
        "total": 3,
        "unknown": 0,
        "missing": 2,
        "stale": 0,
    }
    (source / "tests/test_acceptance.py").write_text("def test_acceptance():\n    assert False\n", encoding="utf-8")
    queried = traceability.query(repo.db, {"import_id": result["import_id"]})
    stale = next(l for l in queried["links"] if l["id"] == link["id"])
    assert stale["state"] == "stale" and stale["explanation"]
    assert queried["coverage"]["verified"] == 0
    assert (
        repo.db.rows("SELECT state FROM requirement_links WHERE id=?", (link["id"],))[0]["state"]
        == "verified"
    )


def test_generic_passed_run_unscoped_and_fake_client_proof_never_verify(workspace):
    first, result = prepared(workspace, mapped=False)
    run_id, task_id = actual_run(workspace, first)
    _root, repo, _source, target = workspace
    params = {
        "import_id": result["import_id"],
        "requirement_id": "REQ-001",
        "kind": "test",
        "target_ref": target,
        "task_id": task_id,
        "module_id": "backend",
        "criteria_ids": ["REQ-001:AC-01"],
        "verification_run_id": run_id,
        "request_id": "LINK-2",
        "expected_version": 0,
        "state": "verified",
        "passed": True,
        "evidence_digest": "fake",
    }
    link = traceability.create_link(repo.db, params)
    assert link["state"] == "mapped"
    assert traceability.coverage(repo.db, result["import_id"])["verified"] == 0


def test_trace_link_scope_version_and_request_key(workspace):
    first, result = prepared(workspace)
    _root, repo, _source, _target = workspace
    task_id = first["selection"]["tasks"][0]["id"]
    params = {
        "import_id": result["import_id"],
        "requirement_id": "REQ-001",
        "kind": "file",
        "target_ref": "tests/test_acceptance.py",
        "task_id": task_id,
        "module_id": "backend",
        "criteria_ids": ["REQ-001:AC-01"],
        "request_id": "LINK-3",
        "expected_version": 0,
    }
    link = traceability.create_link(repo.db, params)
    with pytest.raises(DomainError):
        traceability.create_link(repo.db, dict(params, target_ref="../secret", request_id="bad"))
    with pytest.raises(DomainError):
        traceability.create_link(repo.db, dict(params, module_id="other", request_id="bad"))
    with pytest.raises(DomainError):
        traceability.create_link(
            repo.db, dict(params, criteria_ids=["REQ-001:AC-99"], request_id="bad")
        )
    with pytest.raises(DomainError):
        traceability.create_link(repo.db, dict(params, criteria_ids=[], request_id="LINK-3"))
    updated = traceability.create_link(
        repo.db,
        dict(params, criteria_ids=[], request_id="LINK-4", expected_version=link["version"]),
    )
    assert updated["version"] == link["version"] + 1
    with pytest.raises(DomainError):
        traceability.create_link(
            repo.db, dict(params, request_id="LINK-5", expected_version=link["version"])
        )


def test_trace_cursor_no_skip_and_backend_missing(workspace):
    _first, result = prepared(workspace)
    _root, repo, _, _ = workspace
    ids = []
    cursor = ""
    while True:
        page = traceability.query(
            repo.db, {"import_id": result["import_id"], "limit": 1, "cursor": cursor}
        )
        ids.extend(l["id"] for l in page["links"])
        cursor = page["next_cursor"]
        assert any(r["requirement_id"] == "REQ-002" for r in page["requirements"])
        if not cursor:
            break
    assert len(ids) == len(set(ids)) == len(repo.db.rows("SELECT id FROM requirement_links"))


@pytest.fixture
def server(workspace):
    web = JourneyWeb(workspace[0])
    server = make_server(web)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, web
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def call(server, method, path, payload=None, headers=None) -> tuple[int, Any]:
    client = http.client.HTTPConnection(*server.server_address, timeout=5)
    headers = dict(headers or {})
    if payload is not None:
        payload = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    client.request(method, path, payload, headers)
    response = client.getresponse()
    raw = response.read()
    result = (
        response.status,
        json.loads(raw)
        if response.getheader("Content-Type", "").startswith("application/json")
        else raw,
    )
    client.close()
    return result


def test_home_read_only_no_checks_and_canonical_selection(server, workspace, monkeypatch):
    _root, _repo, _source, _target = workspace

    def forbidden(*args, **kwargs):
        raise AssertionError("GET invoked subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    code, data = call(server[0], "GET", "/api/home/bootstrap?project_id=P1")
    assert code == 200 and data["selected"]["id"] == "P1"
    assert {r["key"] for r in data["readiness"]} == {"folders", "ai", "verification", "mcp"}
    assert len(data["primary_action"]) > 0
    assert next(r for r in data["readiness"] if r["key"] == "ai")["status"] == "unknown"
    assert all("reason" in r and "checked_at" in r for r in data["readiness"])
    code, _ = call(server[0], "GET", "/api/home/bootstrap?project_id=unknown")
    assert code == 400


def test_uninitialized_home_does_not_create_database(tmp_path):
    web = JourneyWeb(tmp_path / "missing")
    result = web.get("bootstrap", {})
    assert result["projects"] == []
    assert not (tmp_path / "missing").exists()


def test_handoff_http_and_path_boundary(server):
    srv, _web = server
    code, boot = call(srv, "GET", "/api/home/bootstrap")
    assert code == 200
    headers = {"X-Local-Token": boot["token"]}
    code, preview = call(
        srv,
        "POST",
        "/api/handoff/preview",
        {"project_id": "P1", "source_studio_id": "STUDIO-001"},
        headers,
    )
    assert code == 200
    params = dict(
        preview["selection"], preview_digest=preview["preview_digest"], request_id="HTTP-1"
    )
    code, result = call(srv, "POST", "/api/handoff/commit", params, headers)
    assert code == 200
    assert result["import_id"]
    assert call(srv, "POST", "/api/traceability/commit", params, headers)[0] == 400
    assert call(srv, "POST", "/api/handoff/preview", params)[0] == 403
    assert (
        call(
            srv,
            "POST",
            "/api/handoff/preview",
            params,
            dict(headers, Origin="https://evil.example"),
        )[0]
        == 403
    )
    assert call(srv, "GET", "/traceability", headers={"Origin": "https://evil.example"})[0] == 403
    assert call(srv, "GET", "/api/home/bootstrap", headers={"Host": "evil.example"})[0] == 403
    for path in ("/home", "/traceability", "/home.js", "/traceability.js", "/journey.css"):
        assert call(srv, "GET", path)[0] == 200


def test_frozen_root_scope_privacy_and_registered_secret(workspace, monkeypatch):
    from agent_dev.adapters import secrets

    _root, repo, source, _target = workspace
    profile = json.loads(
        repo.db.rows("SELECT content_json FROM project_profile_versions")[0]["content_json"]
    )
    profile["repositories"]["backend"]["allowed_paths"] = ["."]
    repo.revise_profile("P1", profile, 0)
    first, result = prepared(workspace)
    task_id = first["selection"]["tasks"][0]["id"]
    secret = "example-registered-private-value"
    monkeypatch.setattr(secrets, "registered_secrets", lambda *a: (secret,))
    (source / "visible.py").write_text('value = "' + secret + '"\n', encoding="utf-8")
    params = {
        "import_id": result["import_id"],
        "requirement_id": "REQ-001",
        "kind": "file",
        "target_ref": "visible.py",
        "task_id": task_id,
        "module_id": "backend",
        "criteria_ids": [],
        "request_id": "VISIBLE",
        "expected_version": 0,
    }
    link = traceability.create_link(repo.db, params)
    detail = traceability.detail(repo.db, link["id"])
    assert secret not in json.dumps(detail)
    assert "****" in json.dumps(detail)
    for relative in (
        ".env",
        ".env.local",
        "credentials/auth.json",
        "private.key",
        ".agent-dev/state.db",
    ):
        path = source / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(secret, encoding="utf-8")
        with pytest.raises(DomainError):
            traceability.create_link(
                repo.db, dict(params, target_ref=relative, request_id="PRIVATE-" + relative)
            )
    (source / "link.py").symlink_to(source / "visible.py")
    with pytest.raises(DomainError):
        traceability.create_link(repo.db, dict(params, target_ref="link.py", request_id="SYMLINK"))


def test_trace_query_bounded_scan_and_cross_project_scope(workspace, monkeypatch):
    _first, result = prepared(workspace)
    _root, repo, _source, _target = workspace
    repo.register_project("P2", "other", {})
    with pytest.raises(DomainError):
        traceability.query(repo.db, {"project_id": "P2", "import_id": result["import_id"]})
    with repo.db.transaction() as conn:
        for i in range(1000):
            conn.execute(
                "INSERT INTO requirement_links(id,import_id,requirement_id,kind,target_ref,state) VALUES(?,?,?,'screen',?,'mapped')",
                (f"Z{i:04d}", result["import_id"], "REQ-001", f"SCR-{i}"),
            )
    evaluated = []
    original = traceability._evaluate

    def evaluate(*args, **kwargs):
        evaluated.append(args[2]["id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(traceability, "_evaluate", evaluate)
    page = traceability.query(repo.db, {"import_id": result["import_id"], "limit": 2})
    assert len(evaluated) <= 2
    assert page["coverage_partial"] and page["unchecked_links"] > 0
    assert page["coverage"]["verified"] == 0
    assert page["next_cursor"]
    evaluated.clear()
    page = traceability.query(
        repo.db, {"import_id": result["import_id"], "limit": 2, "state": "verified"}
    )
    assert len(evaluated) <= 8
    assert page["truncated"] and page["next_cursor"]


def test_trace_request_fingerprints_same_task_once_without_cross_request_cache(
    workspace, monkeypatch
):
    root, repo, source, target = workspace
    initial = studio_handoff.preview(
        repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"}
    )
    tasks = initial["selection"]["tasks"]
    tasks[0]["requirement_ids"] = ["REQ-001", "REQ-002"]
    tasks[0]["verification_mapping"] = [
        {
            "requirement_id": rid,
            "criteria_ids": [rid + ":AC-01"],
            "gate": "acceptance",
            "target": target,
        }
        for rid in ("REQ-001", "REQ-002")
    ]
    first = studio_handoff.preview(repo.db, root, dict(initial["selection"], tasks=tasks))
    result = studio_handoff.commit(
        repo.db,
        root,
        dict(first["selection"], preview_digest=first["preview_digest"], request_id="CACHE-IMPORT"),
    )
    repo.approve("design", result["design_id"], 1, "user")
    repo.approve("plan", result["plan_id"], 1, "user")
    run, task = actual_run(workspace, first)
    for rid in ("REQ-001", "REQ-002"):
        traceability.create_link(
            repo.db,
            {
                "import_id": result["import_id"],
                "requirement_id": rid,
                "kind": "test",
                "target_ref": target,
                "task_id": task,
                "module_id": "backend",
                "criteria_ids": [rid + ":AC-01"],
                "verification_run_id": run,
                "request_id": rid,
                "expected_version": 0,
            },
        )
    calls = []
    original = traceability.evidence_digest

    def fingerprint(bindings):
        calls.append(1)
        return original(bindings)

    monkeypatch.setattr(traceability, "evidence_digest", fingerprint)
    assert (
        traceability.query(repo.db, {"import_id": result["import_id"]})["coverage"]["verified"] == 2
    )
    assert len(calls) == 1
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
            "--allow-empty",
            "-qm",
            "new revision",
        ],
        check=True,
    )
    assert (
        traceability.query(repo.db, {"import_id": result["import_id"]})["coverage"]["verified"] == 0
    )
    assert len(calls) == 2
    (source / "tests/test_acceptance.py").write_text("def test_acceptance():\n    assert False\n", encoding="utf-8")
    assert (
        traceability.query(repo.db, {"import_id": result["import_id"]})["coverage"]["verified"] == 0
    )
    assert len(calls) == 3


def test_commit_changed_after_verification_before_link_cannot_be_verified(workspace):
    first, result = prepared(workspace)
    run_id, task_id = actual_run(workspace, first)
    _root, repo, source, target = workspace
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
            "--allow-empty",
            "-qm",
            "after verification",
        ],
        check=True,
    )
    link = traceability.create_link(
        repo.db,
        {
            "import_id": result["import_id"],
            "requirement_id": "REQ-001",
            "kind": "test",
            "target_ref": target,
            "task_id": task_id,
            "module_id": "backend",
            "criteria_ids": ["REQ-001:AC-01"],
            "verification_run_id": run_id,
            "request_id": "COMMIT-CHANGED",
            "expected_version": 0,
        },
    )
    assert link["state"] != "verified"
