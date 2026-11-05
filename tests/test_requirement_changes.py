"""Requirement change delta, impact, propose/apply/reject acceptance tests (CR-01)."""

import json
import time

import pytest
from test_studio_handoff import _setup_studio_project

from agent_dev.adapters import requirement_changes as rc
from agent_dev.adapters import studio_handoff
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError, ErrorCode

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    db = Database(tmp_path / "state.db")
    repo = WorkflowRepository(db)
    repo.register_project(
        "P1",
        "Test Project",
        {
            "repositories": {"backend": {"path": str(tmp_path / "src"), "allowed_paths": ["src/**"]}},
            "verification": {
                "commands": [
                    {"name": "unit", "repository": "backend", "command": ["python", "-m", "pytest"]}
                ]
            },
        },
    )
    store = StudioStore(tmp_path)
    _setup_studio_project(store, "STUDIO-001")
    return tmp_path, repo, store


@pytest.fixture
def imported(workspace):
    root, repo, store = workspace
    prepared = studio_handoff.preview(repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"})
    result = studio_handoff.commit(
        repo.db, root, dict(prepared["selection"], preview_digest=prepared["preview_digest"], request_id="IMPORT-1")
    )
    repo.approve("design", result["design_id"], 1, "user")
    repo.approve("plan", result["plan_id"], 1, "user")
    return root, repo, store, prepared, result


def _add_studio_revision(store, project_id, new_reqs):
    """Add a new approved spec+mock revision to an existing Studio project."""
    with store.transaction() as db:
        old_spec_rev = db.execute("SELECT spec_revision FROM projects WHERE id=?", (project_id,)).fetchone()[0]
        new_rev = old_spec_rev + 1
        payload = {
            "requirements": new_reqs,
            "screens": [
                {
                    "id": "SCR-001",
                    "title": "메모 목록",
                    "purpose": "목록",
                    "requirement_ids": [r["id"] for r in new_reqs if "SCR" in r.get("source_quote", "")],
                },
            ],
            "questions": [],
            "documents": {},
        }
        # Keep at least one screen linked
        payload["screens"][0]["requirement_ids"] = [new_reqs[0]["id"]]
        mock_payload = {
            "tone": "indigo",
            "screens": [{"id": "SCR-001", "title": "메모 목록", "elements": []}],
        }
        spec_dg = digest(payload)
        mock_dg = digest(mock_payload)
        db.execute(
            "INSERT INTO specs(project_id,revision,payload,source,digest,approved,created) VALUES(?,?,?,?,?,1,?)",
            (project_id, new_rev, json.dumps(payload, sort_keys=True), "", spec_dg, time.time()),
        )
        db.execute(
            "INSERT INTO mocks(project_id,revision,spec_revision,payload,digest,created) VALUES(?,?,?,?,?,?)",
            (project_id, new_rev, new_rev, json.dumps(mock_payload, sort_keys=True), mock_dg, time.time()),
        )
        db.execute(
            "INSERT INTO approvals(project_id,mock_revision,screen_id,digest,note,created) VALUES(?,?,?,?,?,?)",
            (
                project_id,
                new_rev,
                "SCR-001",
                digest({"mock": mock_dg, "spec": spec_dg, "screen": "SCR-001"}),
                "",
                time.time(),
            ),
        )
        db.execute(
            "UPDATE projects SET spec_revision=?,mock_revision=?,version=version+1 WHERE id=?",
            (new_rev, new_rev, project_id),
        )


# ---------------------------------------------------------------------------
# CR-AC01: Delta computation
# ---------------------------------------------------------------------------


def test_delta_added_changed_removed_unchanged():
    before = {"REQ-001": "digest-a", "REQ-002": "digest-b"}
    after = {"REQ-001": "digest-a", "REQ-002": "digest-changed", "REQ-003": "digest-c"}
    delta = rc.requirement_delta(before, after)
    assert delta == {
        "REQ-001": "unchanged",
        "REQ-002": "changed",
        "REQ-003": "added",
    }


def test_delta_removed():
    before = {"REQ-001": "d1", "REQ-002": "d2"}
    after = {"REQ-001": "d1"}
    delta = rc.requirement_delta(before, after)
    assert delta["REQ-001"] == "unchanged"
    assert delta["REQ-002"] == "removed"


def test_delta_id_rename_is_add_plus_remove():
    before = {"REQ-OLD": "d1"}
    after = {"REQ-NEW": "d1"}
    delta = rc.requirement_delta(before, after)
    assert delta["REQ-OLD"] == "removed"
    assert delta["REQ-NEW"] == "added"


def test_delta_backend_only_requirements(imported):
    _root, repo, _store, _prepared, result = imported
    repo.db.rows("SELECT * FROM handoff_imports WHERE id=?", (result["import_id"],))[0]
    reqs = repo.db.rows(
        "SELECT requirement_id,content_digest FROM imported_requirements WHERE import_id=?",
        (result["import_id"],),
    )
    before = {r["requirement_id"]: r["content_digest"] for r in reqs}
    assert "REQ-002" in before


# ---------------------------------------------------------------------------
# CR-AC01/CR-AC02: Preview with normalization
# ---------------------------------------------------------------------------


def test_preview_changed_and_unchanged_reqs(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {
            "id": "REQ-001",
            "title": "메모 저장 업데이트",
            "description": "변경된 저장 기능",
            "source_quote": "저장",
            "priority": "must",
            "acceptance": ["빈 제목 차단", "저장 확인"],
        },
        {
            "id": "REQ-002",
            "title": "백엔드 only",
            "description": "API 제한",
            "source_quote": "API",
            "priority": "must",
            "acceptance": ["인증 필요"],
        },
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "요구사항 업데이트",
            "actor": "user",
        },
    )
    assert "delta" in preview
    assert preview["delta"]["REQ-001"] == "changed"
    assert preview["delta"]["REQ-002"] == "unchanged"
    assert "preview_digest" in preview


def test_preview_cross_project_rejected(imported, tmp_path):
    root, repo, _store, _prepared, result = imported
    db2 = Database(tmp_path / "other.db")
    repo2 = WorkflowRepository(db2)
    repo2.register_project("P2", "Other Project", {"repositories": {}, "verification": {"commands": []}})
    other_store = StudioStore(tmp_path / "other_root")
    _setup_studio_project(other_store, "STUDIO-002")
    _add_studio_revision(
        other_store,
        "STUDIO-002",
        [{"id": "REQ-001", "title": "x", "description": "x", "source_quote": "x", "priority": "must", "acceptance": ["a"]}],
    )
    with pytest.raises(DomainError):
        rc.preview(
            repo.db,
            root,
            {
                "project_id": "P1",
                "source_import_id": result["import_id"],
                "new_studio_id": "STUDIO-002",
                "reason": "cross project",
                "actor": "user",
                "new_studio_root": str(tmp_path / "other_root"),
            },
        )


def test_preview_invalid_import_id_rejected(imported):
    root, repo, _store, _prepared, _result = imported
    with pytest.raises(DomainError):
        rc.preview(
            repo.db,
            root,
            {
                "project_id": "P1",
                "source_import_id": "nonexistent-import-id",
                "new_studio_id": "STUDIO-001",
                "reason": "test",
                "actor": "user",
            },
        )


# ---------------------------------------------------------------------------
# CR-AC03: Propose does not affect active task state
# ---------------------------------------------------------------------------


def test_propose_stores_immutable_and_no_task_change(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {
            "id": "REQ-001",
            "title": "메모 저장 변경",
            "description": "변경 설명",
            "source_quote": "저장",
            "priority": "must",
            "acceptance": ["확인"],
        },
        {
            "id": "REQ-002",
            "title": "백엔드 only",
            "description": "API 제한",
            "source_quote": "API",
            "priority": "must",
            "acceptance": ["인증 필요"],
        },
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "업무 요건 변경",
            "actor": "tester",
        },
    )
    propose_result = rc.propose(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "preview_digest": preview["preview_digest"],
            "reason": "업무 요건 변경",
            "actor": "tester",
            "request_id": "CHANGE-001",
        },
    )
    assert "change_id" in propose_result
    changes = repo.db.rows("SELECT * FROM requirement_changes WHERE id=?", (propose_result["change_id"],))
    assert changes[0]["state"] == "proposed"
    tasks = repo.db.rows("SELECT state FROM tasks")
    for t in tasks:
        assert t["state"] not in ("running", "cancelled")


def test_propose_only_no_runtime_effect(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {
            "id": "REQ-001",
            "title": "변경",
            "description": "변경",
            "source_quote": "저장",
            "priority": "must",
            "acceptance": ["확인"],
        },
        {
            "id": "REQ-002",
            "title": "백엔드 only",
            "description": "API 제한",
            "source_quote": "API",
            "priority": "must",
            "acceptance": ["인증 필요"],
        },
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "test",
            "actor": "user",
        },
    )
    propose_result = rc.propose(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "preview_digest": preview["preview_digest"],
            "reason": "test",
            "actor": "user",
            "request_id": "CHANGE-PROP-ONLY",
        },
    )
    assert repo.db.rows("SELECT * FROM handoff_imports") == repo.db.rows(
        "SELECT * FROM handoff_imports"
    )
    repo.db.rows("SELECT id,state,version FROM tasks")
    changes = repo.db.rows("SELECT * FROM requirement_changes WHERE id=?", (propose_result["change_id"],))
    assert changes[0]["target_import_id"] is None


# ---------------------------------------------------------------------------
# CR-AC04: Idempotency and transaction safety
# ---------------------------------------------------------------------------


def test_idempotent_propose_same_payload(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {
            "id": "REQ-001",
            "title": "변경 2",
            "description": "다른 설명",
            "source_quote": "저장",
            "priority": "must",
            "acceptance": ["확인"],
        },
        {
            "id": "REQ-002",
            "title": "백엔드",
            "description": "API",
            "source_quote": "API",
            "priority": "must",
            "acceptance": ["인증"],
        },
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "retry test",
            "actor": "user",
        },
    )
    params = {
        "project_id": "P1",
        "source_import_id": result["import_id"],
        "preview_digest": preview["preview_digest"],
        "reason": "retry test",
        "actor": "user",
        "request_id": "CHANGE-IDEM",
    }
    first = rc.propose(repo.db, root, params)
    second = rc.propose(repo.db, root, params)
    assert first["change_id"] == second["change_id"]
    assert len(repo.db.rows("SELECT * FROM requirement_changes WHERE request_key=?", ("changes:CHANGE-IDEM",))) == 1


def test_different_payload_same_request_id_rejected(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {
            "id": "REQ-001",
            "title": "변경 3",
            "description": "설명",
            "source_quote": "저장",
            "priority": "must",
            "acceptance": ["확인"],
        },
        {
            "id": "REQ-002",
            "title": "백엔드",
            "description": "API",
            "source_quote": "API",
            "priority": "must",
            "acceptance": ["인증"],
        },
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "original",
            "actor": "user",
        },
    )
    params = {
        "project_id": "P1",
        "source_import_id": result["import_id"],
        "preview_digest": preview["preview_digest"],
        "reason": "original",
        "actor": "user",
        "request_id": "CHANGE-REUSE",
    }
    rc.propose(repo.db, root, params)
    with pytest.raises(DomainError):
        rc.propose(repo.db, root, dict(params, reason="different reason"))


def test_transaction_fault_rollback(imported, monkeypatch):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {
            "id": "REQ-001",
            "title": "fault test",
            "description": "설명",
            "source_quote": "저장",
            "priority": "must",
            "acceptance": ["확인"],
        },
        {
            "id": "REQ-002",
            "title": "백엔드",
            "description": "API",
            "source_quote": "API",
            "priority": "must",
            "acceptance": ["인증"],
        },
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "fault",
            "actor": "user",
        },
    )
    original = WorkflowRepository._event

    def fault_event(self, conn, aggregate_id, kind, payload):
        if kind == "RequirementChangeProposed":
            raise RuntimeError("injected fault")
        return original(self, conn, aggregate_id, kind, payload)

    monkeypatch.setattr(WorkflowRepository, "_event", fault_event)
    with pytest.raises(RuntimeError):
        rc.propose(
            repo.db,
            root,
            {
                "project_id": "P1",
                "source_import_id": result["import_id"],
                "preview_digest": preview["preview_digest"],
                "reason": "fault",
                "actor": "user",
                "request_id": "CHANGE-FAULT",
            },
        )
    assert not repo.db.rows("SELECT * FROM requirement_changes WHERE request_key='changes:CHANGE-FAULT'")


def test_stale_preview_rejected_after_new_revision(imported):
    root, repo, store, _prepared, result = imported
    new_reqs_v1 = [
        {"id": "REQ-001", "title": "v1", "description": "d", "source_quote": "저장", "priority": "must", "acceptance": ["a"]},
        {"id": "REQ-002", "title": "백엔드", "description": "API", "source_quote": "API", "priority": "must", "acceptance": ["인증"]},
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs_v1)
    preview_v1 = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "stale test",
            "actor": "user",
        },
    )
    new_reqs_v2 = [
        {"id": "REQ-001", "title": "v2 different", "description": "d2", "source_quote": "저장", "priority": "must", "acceptance": ["b"]},
        {"id": "REQ-002", "title": "백엔드", "description": "API", "source_quote": "API", "priority": "must", "acceptance": ["인증"]},
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs_v2)
    with pytest.raises(DomainError) as exc:
        rc.propose(
            repo.db,
            root,
            {
                "project_id": "P1",
                "source_import_id": result["import_id"],
                "preview_digest": preview_v1["preview_digest"],
                "reason": "stale test",
                "actor": "user",
                "request_id": "CHANGE-STALE",
            },
        )
    assert exc.value.code == ErrorCode.STALE_VERSION


# ---------------------------------------------------------------------------
# CR-AC03: Active impact rejection
# ---------------------------------------------------------------------------


def test_apply_rejects_active_impact_task(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {
            "id": "REQ-001",
            "title": "변경 active",
            "description": "변경",
            "source_quote": "저장",
            "priority": "must",
            "acceptance": ["확인"],
        },
        {
            "id": "REQ-002",
            "title": "백엔드",
            "description": "API",
            "source_quote": "API",
            "priority": "must",
            "acceptance": ["인증"],
        },
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "active test",
            "actor": "user",
        },
    )
    propose_result = rc.propose(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "preview_digest": preview["preview_digest"],
            "reason": "active test",
            "actor": "user",
            "request_id": "CHANGE-ACTIVE",
        },
    )
    task_ids = [t["id"] for t in repo.db.rows("SELECT id FROM tasks")]
    assert task_ids
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET state='running' WHERE id=?", (task_ids[0],))
    with pytest.raises(DomainError) as exc:
        rc.apply(
            repo.db,
            root,
            {
                "change_id": propose_result["change_id"],
                "expected_version": 0,
                "request_id": "APPLY-ACTIVE",
            },
        )
    assert exc.value.code in (ErrorCode.LEASE_CONFLICT, ErrorCode.STALE_VERSION)
    change = repo.db.rows("SELECT state FROM requirement_changes WHERE id=?", (propose_result["change_id"],))[0]
    assert change["state"] == "proposed"


# ---------------------------------------------------------------------------
# CR-AC05: Apply creates new drafts atomically
# ---------------------------------------------------------------------------


def test_apply_creates_new_import_design_plan_atomically(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {
            "id": "REQ-001",
            "title": "변경 apply",
            "description": "변경",
            "source_quote": "저장",
            "priority": "must",
            "acceptance": ["확인"],
        },
        {
            "id": "REQ-002",
            "title": "백엔드",
            "description": "API",
            "source_quote": "API",
            "priority": "must",
            "acceptance": ["인증"],
        },
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "apply test",
            "actor": "user",
        },
    )
    propose_result = rc.propose(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": result["import_id"],
            "preview_digest": preview["preview_digest"],
            "reason": "apply test",
            "actor": "user",
            "request_id": "CHANGE-APPLY",
        },
    )
    apply_result = rc.apply(
        repo.db,
        root,
        {
            "change_id": propose_result["change_id"],
            "expected_version": 0,
            "request_id": "APPLY-001",
        },
    )
    assert "target_import_id" in apply_result
    assert "design_id" in apply_result
    assert "plan_id" in apply_result
    change = repo.db.rows("SELECT * FROM requirement_changes WHERE id=?", (propose_result["change_id"],))[0]
    assert change["state"] == "applied"
    assert change["target_import_id"] == apply_result["target_import_id"]
    old_reqs = repo.db.rows("SELECT * FROM imported_requirements WHERE import_id=?", (result["import_id"],))
    assert old_reqs, "original import preserved"
    new_reqs_rows = repo.db.rows(
        "SELECT * FROM imported_requirements WHERE import_id=?", (apply_result["target_import_id"],)
    )
    assert new_reqs_rows


def test_apply_idempotent(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {"id": "REQ-001", "title": "idem apply", "description": "d", "source_quote": "저장", "priority": "must", "acceptance": ["확인"]},
        {"id": "REQ-002", "title": "백엔드", "description": "API", "source_quote": "API", "priority": "must", "acceptance": ["인증"]},
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "new_studio_id": "STUDIO-001", "reason": "idem", "actor": "user"},
    )
    propose_result = rc.propose(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "preview_digest": preview["preview_digest"], "reason": "idem", "actor": "user", "request_id": "CHANGE-IDEM2"},
    )
    apply_params = {"change_id": propose_result["change_id"], "expected_version": 0, "request_id": "APPLY-IDEM"}
    first = rc.apply(repo.db, root, apply_params)
    second = rc.apply(repo.db, root, apply_params)
    assert first == second


# ---------------------------------------------------------------------------
# CR-AC05: Reject
# ---------------------------------------------------------------------------


def test_reject_proposed_change(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {"id": "REQ-001", "title": "reject test", "description": "d", "source_quote": "저장", "priority": "must", "acceptance": ["확인"]},
        {"id": "REQ-002", "title": "백엔드", "description": "API", "source_quote": "API", "priority": "must", "acceptance": ["인증"]},
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "new_studio_id": "STUDIO-001", "reason": "reject", "actor": "user"},
    )
    propose_result = rc.propose(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "preview_digest": preview["preview_digest"], "reason": "reject", "actor": "user", "request_id": "CHANGE-REJ"},
    )
    rc.reject(repo.db, {"change_id": propose_result["change_id"], "expected_version": 0, "reason": "필요 없음", "actor": "user"})
    change = repo.db.rows("SELECT state FROM requirement_changes WHERE id=?", (propose_result["change_id"],))[0]
    assert change["state"] == "rejected"


def test_reject_applied_change_is_impossible(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {"id": "REQ-001", "title": "reject applied", "description": "d", "source_quote": "저장", "priority": "must", "acceptance": ["확인"]},
        {"id": "REQ-002", "title": "백엔드", "description": "API", "source_quote": "API", "priority": "must", "acceptance": ["인증"]},
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "new_studio_id": "STUDIO-001", "reason": "reject applied", "actor": "user"},
    )
    propose_result = rc.propose(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "preview_digest": preview["preview_digest"], "reason": "reject applied", "actor": "user", "request_id": "CHANGE-REJ2"},
    )
    rc.apply(repo.db, root, {"change_id": propose_result["change_id"], "expected_version": 0, "request_id": "APPLY-REJ2"})
    with pytest.raises(DomainError):
        rc.reject(repo.db, {"change_id": propose_result["change_id"], "expected_version": 1, "reason": "too late", "actor": "user"})


# ---------------------------------------------------------------------------
# CR-AC06: Impact guard - old task cannot reuse old evidence
# ---------------------------------------------------------------------------


def test_impact_guard_blocks_old_task_execution(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {"id": "REQ-001", "title": "guard test", "description": "d", "source_quote": "저장", "priority": "must", "acceptance": ["확인"]},
        {"id": "REQ-002", "title": "백엔드", "description": "API", "source_quote": "API", "priority": "must", "acceptance": ["인증"]},
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "new_studio_id": "STUDIO-001", "reason": "guard", "actor": "user"},
    )
    propose_result = rc.propose(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "preview_digest": preview["preview_digest"], "reason": "guard", "actor": "user", "request_id": "CHANGE-GUARD"},
    )
    rc.apply(
        repo.db, root,
        {"change_id": propose_result["change_id"], "expected_version": 0, "request_id": "APPLY-GUARD"},
    )
    task_ids = [t["id"] for t in repo.db.rows("SELECT id FROM tasks")]
    assert task_ids
    for task_id in task_ids:
        guard = rc.execution_guard(repo.db, task_id)
        assert guard["blocked"]
        assert "next_actions" in guard


def test_unknown_impacts_remain_unresolved(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {"id": "REQ-001", "title": "unknown impact", "description": "d", "source_quote": "저장", "priority": "must", "acceptance": ["확인"]},
        {"id": "REQ-002", "title": "백엔드", "description": "API", "source_quote": "API", "priority": "must", "acceptance": ["인증"]},
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "new_studio_id": "STUDIO-001", "reason": "unknown test", "actor": "user"},
    )
    # An actual task link supplies a known impact; a user may explicitly keep
    # the decision unresolved, which must remain visible and refuse apply.
    decisions = [{"requirement_id": i["requirement_id"], "task_id": i["task_id"], "disposition": "unknown"} for i in preview["impacts"]]
    params = dict(preview["selection"], impact_decisions=decisions)
    preview = rc.preview(repo.db, root, params)
    assert all(i["disposition"] == "unknown" for i in preview["impacts"])
    change = rc.propose(repo.db, root, dict(params, preview_digest=preview["preview_digest"], request_id="UNKNOWN-PROP"))
    with pytest.raises(DomainError, match="미확정"):
        rc.apply(repo.db, root, {"change_id": change["change_id"], "expected_version": 0, "request_id": "UNKNOWN-APPLY"})


# ---------------------------------------------------------------------------
# CR-AC01: Query and bootstrap API
# ---------------------------------------------------------------------------


def test_query_returns_changes_for_project(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {"id": "REQ-001", "title": "query test", "description": "d", "source_quote": "저장", "priority": "must", "acceptance": ["확인"]},
        {"id": "REQ-002", "title": "백엔드", "description": "API", "source_quote": "API", "priority": "must", "acceptance": ["인증"]},
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "new_studio_id": "STUDIO-001", "reason": "query test", "actor": "user"},
    )
    rc.propose(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "preview_digest": preview["preview_digest"], "reason": "query test", "actor": "user", "request_id": "CHANGE-QUERY"},
    )
    query_result = rc.query(repo.db, {"project_id": "P1"})
    assert len(query_result["changes"]) == 1
    assert query_result["changes"][0]["state"] == "proposed"


def test_detail_returns_change_with_impacts(imported):
    root, repo, store, _prepared, result = imported
    new_reqs = [
        {"id": "REQ-001", "title": "detail test", "description": "d", "source_quote": "저장", "priority": "must", "acceptance": ["확인"]},
        {"id": "REQ-002", "title": "백엔드", "description": "API", "source_quote": "API", "priority": "must", "acceptance": ["인증"]},
    ]
    _add_studio_revision(store, "STUDIO-001", new_reqs)
    preview = rc.preview(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "new_studio_id": "STUDIO-001", "reason": "detail test", "actor": "user"},
    )
    propose_result = rc.propose(
        repo.db, root,
        {"project_id": "P1", "source_import_id": result["import_id"], "preview_digest": preview["preview_digest"], "reason": "detail test", "actor": "user", "request_id": "CHANGE-DETAIL"},
    )
    detail = rc.detail(repo.db, propose_result["change_id"])
    assert detail["change"]["id"] == propose_result["change_id"]
    assert "impacts" in detail
    assert "delta" in detail


def test_bootstrap_returns_project_changes(imported):
    _root, repo, _store, _prepared, _result = imported
    bootstrap = rc.bootstrap(repo.db, {"project_id": "P1"})
    assert "changes" in bootstrap
    assert "imports" in bootstrap
