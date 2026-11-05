"""Actual host, approval, source ownership and coverage regressions for CR01."""

import contextlib
import json
import sqlite3

import pytest
from test_requirement_changes import _add_studio_revision, workspace
from test_requirement_changes import imported as _imported_fixture
from test_workflow_runtime import Runner
from test_workflow_runtime import runtime as _runtime_fixture

from agent_dev.adapters import requirement_changes as rc
from agent_dev.adapters import studio_handoff as sh
from agent_dev.adapters import traceability
from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.domain.errors import DomainError


@pytest.fixture
def imported(workspace):
    return _imported_fixture.__wrapped__(workspace)


@pytest.fixture
def runtime(tmp_path):
    return _runtime_fixture.__wrapped__(tmp_path)


def proposed(imported):
    root, repo, store, _, old = imported
    with store.connect() as s:
        reqs = json.loads(
            s.execute(
                "SELECT payload FROM specs WHERE project_id='STUDIO-001' ORDER BY revision DESC LIMIT 1"
            ).fetchone()[0]
        )["requirements"]
    reqs[0]["title"] += " changed"
    _add_studio_revision(store, "STUDIO-001", reqs)
    p = {
        "project_id": "P1",
        "source_import_id": old["import_id"],
        "new_studio_id": "STUDIO-001",
        "reason": "SPEC change",
        "actor": "reviewer",
    }
    preview = rc.preview(repo.db, root, p)
    change = rc.propose(
        repo.db, root, dict(p, preview_digest=preview["preview_digest"], request_id="SPEC-PROP")
    )
    return root, repo, store, old, preview, change


def apply(args):
    root, repo, _, _, _, change = args
    return rc.apply(
        repo.db,
        root,
        {"change_id": change["change_id"], "expected_version": 0, "request_id": "SPEC-APPLY"},
    )


def test_approval_materializes_links_followup_and_frozen_scope(imported):
    args = proposed(imported)
    _root, repo, _, _, _, change = args
    result = apply(args)
    assert not repo.db.rows("SELECT id FROM tasks WHERE plan_id=?", (result["plan_id"],))
    repo.approve("design", result["design_id"], 1, "reviewer")
    repo.approve("plan", result["plan_id"], 1, "reviewer")
    tasks = repo.db.rows("SELECT * FROM tasks WHERE plan_id=?", (result["plan_id"],))
    links = repo.db.rows(
        "SELECT * FROM requirement_links WHERE import_id=?", (result["target_import_id"],)
    )
    impacts = repo.db.rows(
        "SELECT * FROM requirement_change_impacts WHERE change_id=?", (change["change_id"],)
    )
    proof = {
        "tasks": len(tasks),
        "links": len(links),
        "followups": [r["followup_task_id"] for r in impacts],
        "scopes": ["handoff_scope" in json.loads(t["definition_json"]) for t in tasks],
    }
    # Authentic materialization only after both explicit approvals.
    assert links and all(r["followup_task_id"] for r in impacts) and all(proof["scopes"]), proof


def test_stale_task_version_is_rejected(imported):
    args = proposed(imported)
    _root, repo, _, _, preview, _ = args
    tid = preview["impacts"][0]["task_id"]
    with repo.db.transaction() as c:
        c.execute("UPDATE tasks SET version=version+1 WHERE id=?", (tid,))
    with pytest.raises(DomainError):
        apply(args)


def test_stale_profile_is_rejected(imported):
    args = proposed(imported)
    repo = args[1]
    profile = json.loads(
        repo.db.rows("SELECT content_json FROM project_profile_versions")[0]["content_json"]
    )
    profile["repositories"]["backend"]["allowed_paths"].append("new/**")
    repo.revise_profile("P1", profile, 0)
    with pytest.raises(DomainError):
        apply(args)


def test_live_lease_is_rejected_even_when_state_not_running(imported):
    args = proposed(imported)
    repo = args[1]
    safety = ExecutionSafety(repo)
    lease = safety.acquire("task:" + args[4]["impacts"][0]["task_id"], "reviewer")
    try:
        with pytest.raises(DomainError):
            apply(args)
    finally:
        safety.release(lease)


def test_studio_writer_owned_through_canonical_commit(imported, monkeypatch):
    args = proposed(imported)
    observed = []
    original = type(args[1])._event

    def event(self, c, aggregate, kind, payload):
        if kind == "RequirementChangeApplied":
            with contextlib.closing(sh._open(sh._studio_db_path(args[0]), writable=True)) as studio:
                studio.execute("PRAGMA busy_timeout=1")
                try:
                    studio.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError:
                    observed.append("locked")
                else:
                    observed.append("writer available before canonical commit")
                    studio.rollback()
        return original(self, c, aggregate, kind, payload)

    monkeypatch.setattr(type(args[1]), "_event", event)
    apply(args)
    assert observed == ["locked"], observed


def test_missing_task_links_require_explicit_unknown_decision(workspace):
    root, repo, store = workspace
    p = sh.preview(repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"})
    old = sh.commit(
        repo.db,
        root,
        dict(p["selection"], preview_digest=p["preview_digest"], request_id="SPEC-IMPORT"),
    )
    # Valid imported draft: no actual tasks yet; plan_task/screen links are real but task_id NULL.
    args = proposed((root, repo, store, p, old))
    assert args[4]["impacts"], args[4]


def test_ui_and_cli_shaped_preview_accepts_selected_import(imported):
    root, repo, _, _, old = imported
    # Exact fields sent by changes.js:285 and command_line.py:693.
    result = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": old["import_id"],
            "reason": "SPEC",
            "actor": "reviewer",
        },
    )
    assert result["preview_digest"]


def test_old_host_runtime_execution_is_blocked(runtime):
    from test_studio_handoff import _setup_studio_project

    store = StudioStore(runtime.project_root)
    _setup_studio_project(store, "STUDIO-001")
    p = sh.preview(
        runtime.store.db,
        runtime.project_root,
        {"project_id": "P", "source_studio_id": "STUDIO-001"},
    )
    old = sh.commit(
        runtime.store.db,
        runtime.project_root,
        dict(p["selection"], preview_digest=p["preview_digest"], request_id="SPEC-RUNTIME-IMPORT"),
    )
    runtime.store.approve("design", old["design_id"], 1, "reviewer")
    runtime.store.approve("plan", old["plan_id"], 1, "reviewer")
    tid = p["selection"]["tasks"][0]["id"]
    runtime.store.create_snapshot(tid, runtime.resolve_snapshot(tid))
    with store.connect() as s:
        reqs = json.loads(
            s.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    reqs[0]["title"] += " changed"
    _add_studio_revision(store, "STUDIO-001", reqs)
    params = {
        "project_id": "P",
        "source_import_id": old["import_id"],
        "new_studio_id": "STUDIO-001",
        "reason": "SPEC host guard",
        "actor": "reviewer",
    }
    preview = rc.preview(runtime.store.db, runtime.project_root, params)
    proposal = rc.propose(
        runtime.store.db,
        runtime.project_root,
        dict(params, preview_digest=preview["preview_digest"], request_id="SPEC-RUNTIME-PROP"),
    )
    rc.apply(
        runtime.store.db,
        runtime.project_root,
        {
            "change_id": proposal["change_id"],
            "expected_version": 0,
            "request_id": "SPEC-RUNTIME-APPLY",
        },
    )
    assert rc.execution_guard(runtime.store.db, tid)["blocked"]
    runner = Runner()
    try:
        result = runtime.execute(tid, "SPEC-RUNTIME-EXECUTE", runner=runner)
    except DomainError:
        assert runner.calls == 0
    else:
        pytest.fail(
            f"Applied old task executed via public runtime: {result}; actual runner.calls={runner.calls}"
        )


def test_apply_exact_retry_works_after_source_removed(imported):
    args = proposed(imported)
    first = apply(args)
    sh._studio_db_path(args[0]).unlink()
    assert apply(args) == first


def test_apply_fault_rolls_back_whole_change(imported, monkeypatch):
    args = proposed(imported)
    repo = args[1]
    before = {
        t: repo.db.rows("SELECT * FROM " + t)
        for t in [
            "handoff_imports",
            "design_documents",
            "task_plans",
            "requirement_links",
            "commands",
            "events",
        ]
    }
    original = type(repo)._event

    def event(self, c, aggregate, kind, payload):
        if kind == "RequirementChangeApplied":
            raise RuntimeError("SPEC injected after handoff writes")
        return original(self, c, aggregate, kind, payload)

    monkeypatch.setattr(type(repo), "_event", event)
    with pytest.raises(RuntimeError):
        apply(args)
    assert (
        repo.db.rows("SELECT state FROM requirement_changes WHERE id=?", (args[5]["change_id"],))[
            0
        ]["state"]
        == "proposed"
    )
    assert before == {t: repo.db.rows("SELECT * FROM " + t) for t in before}


def test_delta_redacts_new_content_exactly_like_import(tmp_path, monkeypatch):
    from agent_dev.adapters import secrets

    root, repo, store = workspace.__wrapped__(tmp_path)
    monkeypatch.setattr(secrets, "registered_secrets", lambda *_: ("example-SPEC-private-value",))
    with store.connect() as s:
        reqs = json.loads(
            s.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    reqs[0]["description"] += " example-SPEC-private-value"
    _add_studio_revision(store, "STUDIO-001", reqs)
    p = sh.preview(repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"})
    old = sh.commit(
        repo.db,
        root,
        dict(p["selection"], preview_digest=p["preview_digest"], request_id="SECRET-IMPORT"),
    )
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": old["import_id"],
            "new_studio_id": "STUDIO-001",
            "reason": "no content change",
            "actor": "reviewer",
        },
    )
    assert preview["delta"]["REQ-001"] == "unchanged", preview["delta"]


def test_applied_changed_requirement_cannot_recover_old_coverage_when_studio_missing(tmp_path):
    from test_project_journey import actual_run, prepared
    from test_project_journey import workspace as jw

    ws = jw.__wrapped__(tmp_path)
    root, repo, _, target = ws
    first, old = prepared(ws)
    run, tid = actual_run(ws, first)
    link = traceability.create_link(
        repo.db,
        {
            "import_id": old["import_id"],
            "requirement_id": "REQ-001",
            "kind": "test",
            "target_ref": target,
            "task_id": tid,
            "module_id": "backend",
            "criteria_ids": ["REQ-001:AC-01"],
            "verification_run_id": run,
            "request_id": "SPEC-REAL-LINK",
            "expected_version": 0,
        },
    )
    assert link["state"] == "verified"
    assert traceability.coverage(repo.db, old["import_id"])["verified"] == 1
    store = StudioStore(root)
    args = proposed((root, repo, store, first, old))
    apply(args)
    before = traceability.query(repo.db, {"project_id": "P1"})
    assert before["coverage"]["total"] == 3
    sh._studio_db_path(root).unlink()
    current = traceability.query(repo.db, {"project_id": "P1"})
    assert current["coverage"]["total"] == 3
    assert next(l for l in current["links"] if l["id"] == link["id"])["state"] == "historical"
    assert current["coverage"]["verified"] == 0, current["coverage"]


def test_missing_draft_links_are_immutable_unknown_and_excludable(workspace):
    root, repo, store = workspace
    first = sh.preview(repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"})
    old = sh.commit(
        repo.db,
        root,
        dict(first["selection"], preview_digest=first["preview_digest"], request_id="DRAFT-IMPORT"),
    )
    args = proposed((root, repo, store, first, old))
    assert args[4]["impacts"][0]["task_id"] is None
    detail = rc.detail(repo.db, args[5]["change_id"])
    assert detail["impacts"][0]["disposition"] == "unknown"
    assert not repo.db.rows(
        "SELECT * FROM requirement_change_impacts WHERE change_id=?", (args[5]["change_id"],)
    )
    with pytest.raises(DomainError, match="미확정"):
        apply(args)
    params = dict(
        args[4]["selection"],
        impact_decisions=[
            {
                "requirement_id": "REQ-001",
                "task_id": None,
                "disposition": "excluded",
                "exclusion_reason": "Existing draft was never executed; implement the current version in the new plan",
            }
        ],
    )
    preview = rc.preview(repo.db, root, params)
    change = rc.propose(
        repo.db,
        root,
        dict(params, preview_digest=preview["preview_digest"], request_id="DRAFT-EXCLUDE"),
    )
    result = rc.apply(
        repo.db,
        root,
        {
            "change_id": change["change_id"],
            "expected_version": 0,
            "request_id": "DRAFT-EXCLUDE-APPLY",
        },
    )
    assert result["state"] == "applied"
    assert not repo.db.rows("SELECT * FROM tasks WHERE plan_id=?", (result["plan_id"],))
    assert rc.detail(repo.db, change["change_id"])["impacts"][0]["exclusion_reason"]


@pytest.mark.parametrize("forgery", ["task", "evidence", "requirement", "followup"])
def test_forged_impact_mapping_is_rejected(imported, forgery):
    root, repo, _, _, preview, _ = proposed(imported)
    impact = preview["impacts"][0]
    decision = {
        "requirement_id": impact["requirement_id"],
        "task_id": impact["task_id"],
        "disposition": "changed",
        "evidence_digest": impact["evidence_digest"],
    }
    params = dict(preview["selection"], impact_decisions=[decision])
    if forgery == "task":
        decision["task_id"] = next(
            r["id"] for r in repo.db.rows("SELECT * FROM tasks") if r["id"] != impact["task_id"]
        )
    elif forgery == "evidence":
        decision["evidence_digest"] = "0" * 64
    elif forgery == "requirement":
        decision["requirement_id"] = "REQ-FORGED"
    else:
        params["followup_mapping"] = [
            {
                "requirement_id": impact["requirement_id"],
                "task_id": impact["task_id"],
                "followup_task_id": impact["task_id"],
            }
        ]
    with pytest.raises(DomainError):
        rc.preview(repo.db, root, params)


def test_scoped_detail_apply_reject_and_query_refuse_unregistered_project(imported):
    args = proposed(imported)
    root, repo, _, _, _, change = args
    for action in (rc.query, rc.bootstrap):
        with pytest.raises(DomainError):
            action(repo.db, {"project_id": "FORGED"})
    with pytest.raises(DomainError):
        rc.detail(repo.db, change["change_id"], "FORGED")
    with pytest.raises(DomainError):
        rc.apply(
            repo.db,
            root,
            {
                "project_id": "FORGED",
                "change_id": change["change_id"],
                "expected_version": 0,
                "request_id": "FORGED-APPLY",
            },
        )
    with pytest.raises(DomainError):
        rc.reject(
            repo.db,
            {
                "project_id": "FORGED",
                "change_id": change["change_id"],
                "expected_version": 0,
                "reason": "x",
                "actor": "reviewer",
            },
        )


def test_approval_materialization_failure_rolls_back_tasks_links_followups_and_approval(
    imported, monkeypatch
):
    args = proposed(imported)
    result = apply(args)
    repo = args[1]
    repo.approve("design", result["design_id"], 1, "reviewer")
    tables = [
        "tasks",
        "task_dependencies",
        "requirement_links",
        "requirement_change_impacts",
        "approvals",
        "task_plans",
        "events",
        "notification_outbox",
    ]
    before = {t: repo.db.rows("SELECT * FROM " + t) for t in tables}
    original = type(repo)._event

    def event(self, conn, aggregate, kind, payload):
        if kind == "PlanTasksMaterialized":
            raise RuntimeError("approval fault after authentic followup FK")
        return original(self, conn, aggregate, kind, payload)

    monkeypatch.setattr(type(repo), "_event", event)
    with pytest.raises(RuntimeError, match="approval fault"):
        repo.approve("plan", result["plan_id"], 1, "reviewer")
    assert before == {t: repo.db.rows("SELECT * FROM " + t) for t in tables}


def test_apply_different_payload_key_reuse_and_different_intent_same_source_conflict(imported):
    args = proposed(imported)
    root, repo, _, _, preview, _ = args
    second = rc.propose(
        repo.db,
        root,
        dict(
            preview["selection"], preview_digest=preview["preview_digest"], request_id="SECOND-PROP"
        ),
    )
    result = apply(args)
    assert result == apply(args)
    with pytest.raises(DomainError):
        rc.apply(
            repo.db,
            root,
            {"change_id": args[5]["change_id"], "expected_version": 1, "request_id": "SPEC-APPLY"},
        )
    with pytest.raises(DomainError):
        rc.apply(
            repo.db,
            root,
            {"change_id": second["change_id"], "expected_version": 0, "request_id": "SECOND-APPLY"},
        )


def test_backend_only_delta_and_id_rename_no_inferred_identity(imported):
    root, repo, store, _, old = imported
    with store.connect() as conn:
        requirements = json.loads(
            conn.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    requirements[0]["id"] = "REQ-RENAMED"
    requirements[1]["description"] += " new backend policy"
    _add_studio_revision(store, "STUDIO-001", requirements)
    preview = rc.preview(
        repo.db,
        root,
        {
            "project_id": "P1",
            "source_import_id": old["import_id"],
            "reason": "rename and backend",
            "actor": "reviewer",
        },
    )
    assert preview["delta"] == {"REQ-001": "removed", "REQ-002": "changed", "REQ-RENAMED": "added"}
    assert {i["requirement_id"] for i in preview["impacts"]} == {"REQ-001", "REQ-002"}


def test_plan_scope_semantic_sets_and_ordered_dependencies(imported):
    root, repo, _, _, preview, _ = proposed(imported)
    params = preview["selection"]
    a = rc.preview(repo.db, root, params)
    for task in params["handoff"]["tasks"]:
        task["requirement_ids"] = list(reversed(task["requirement_ids"]))
        task["verification_gates"] = list(reversed(task["verification_gates"]))
        for mapping in task["verification_mapping"]:
            mapping["criteria_ids"] = list(reversed(mapping["criteria_ids"]))
    b = rc.preview(repo.db, root, params)
    assert a["preview_digest"] == b["preview_digest"]


def test_direct_verification_gate_review_publish_and_approval_guards(imported):
    from types import SimpleNamespace

    from agent_dev.adapters.phase_catalog import SnapshotPhaseCatalog
    from agent_dev.adapters.publishing import PublishAdapter
    from agent_dev.adapters.verification import VerificationAdapter

    args = proposed(imported)
    root, repo, _, _, preview, _ = args
    apply(args)
    tid = preview["impacts"][0]["task_id"]
    adapter = PublishAdapter(SimpleNamespace(store=repo))
    operations = [
        lambda: VerificationAdapter(repo).verify(tid, {}),
        lambda: SnapshotPhaseCatalog(repo, root).verify_node(tid, [], {}),
        lambda: adapter.publish(tid, "backend", "blocked", dry_run=True),
        lambda: adapter.review(tid),
        lambda: adapter.complete(tid, "none", "reviewer"),
        lambda: repo.approve_node(
            tid, "none", "reviewer", "approved", 0, {"resource": "task:" + tid}
        ),
    ]
    for operation in operations:
        with pytest.raises(DomainError, match="적용된 요구사항 변경"):
            operation()


def test_proposed_and_reasoned_excluded_have_no_execution_guard_effect(imported):
    root, repo, _, _, preview, _ = proposed(imported)
    tid = preview["impacts"][0]["task_id"]
    assert not rc.execution_guard(repo.db, tid)["blocked"]
    params = dict(
        preview["selection"],
        impact_decisions=[
            {
                "requirement_id": "REQ-001",
                "task_id": tid,
                "disposition": "excluded",
                "exclusion_reason": "Old task retained only for an independent legacy release; new requirement handled separately",
            }
        ],
    )
    p = rc.preview(repo.db, root, params)
    c = rc.propose(
        repo.db, root, dict(params, preview_digest=p["preview_digest"], request_id="EXCLUDE-PROP")
    )
    rc.apply(
        repo.db,
        root,
        {"change_id": c["change_id"], "expected_version": 0, "request_id": "EXCLUDE-APPLY"},
    )
    assert not rc.execution_guard(repo.db, tid)["blocked"]


def test_unchanged_authoritative_proof_kept_and_not_double_counted(tmp_path):
    from test_project_journey import actual_run, prepared
    from test_project_journey import workspace as jw

    ws = jw.__wrapped__(tmp_path)
    root, repo, _, target = ws
    first, old = prepared(ws)
    run, tid = actual_run(ws, first)
    link = traceability.create_link(
        repo.db,
        {
            "import_id": old["import_id"],
            "requirement_id": "REQ-001",
            "kind": "test",
            "target_ref": target,
            "task_id": tid,
            "module_id": "backend",
            "criteria_ids": ["REQ-001:AC-01"],
            "verification_run_id": run,
            "request_id": "UNCHANGED-PROOF",
            "expected_version": 0,
        },
    )
    store = StudioStore(root)
    with store.connect() as conn:
        reqs = json.loads(
            conn.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    reqs[1]["description"] += " changed backend policy"
    _add_studio_revision(store, "STUDIO-001", reqs)
    params = {
        "project_id": "P1",
        "source_import_id": old["import_id"],
        "reason": "backend only",
        "actor": "reviewer",
    }
    preview = rc.preview(repo.db, root, params)
    change = rc.propose(
        repo.db,
        root,
        dict(params, preview_digest=preview["preview_digest"], request_id="UNCHANGED-PROP"),
    )
    rc.apply(
        repo.db,
        root,
        {"change_id": change["change_id"], "expected_version": 0, "request_id": "UNCHANGED-APPLY"},
    )
    for missing in [False, True]:
        if missing:
            sh._studio_db_path(root).unlink()
        result = traceability.query(repo.db, {"project_id": "P1"})
        assert result["coverage"]["total"] == 3
        assert result["coverage"]["verified"] == 1
        assert next(l for l in result["links"] if l["id"] == link["id"])["state"] == "verified"


def test_normal_handoff_payload_cannot_lower_full_requirement_validation(workspace):
    root, repo, _ = workspace
    p = sh.preview(repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"})
    params = dict(
        p["selection"], tasks=p["selection"]["tasks"][:1], required_requirement_ids=["REQ-001"]
    )
    with pytest.raises(DomainError, match="모든 요구사항"):
        sh.preview(repo.db, root, params)


def runtime_proposal(rt):
    from test_studio_handoff import _setup_studio_project

    store = StudioStore(rt.project_root)
    _setup_studio_project(store, "STUDIO-001")
    first = sh.preview(
        rt.store.db, rt.project_root, {"project_id": "P", "source_studio_id": "STUDIO-001"}
    )
    old = sh.commit(
        rt.store.db,
        rt.project_root,
        dict(first["selection"], preview_digest=first["preview_digest"], request_id="HOST-IMPORT"),
    )
    rt.store.approve("design", old["design_id"], 1, "reviewer")
    rt.store.approve("plan", old["plan_id"], 1, "reviewer")
    tid = first["selection"]["tasks"][0]["id"]
    rt.store.create_snapshot(tid, rt.resolve_snapshot(tid))
    with store.connect() as conn:
        reqs = json.loads(
            conn.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    reqs[0]["title"] += " changed"
    _add_studio_revision(store, "STUDIO-001", reqs)
    params = {
        "project_id": "P",
        "source_import_id": old["import_id"],
        "reason": "host contract",
        "actor": "reviewer",
    }
    return tid, params


@pytest.mark.parametrize("excluded", [False, True])
def test_actual_host_proposal_and_reasoned_exclusion_allow_existing_execution(runtime, excluded):
    tid, params = runtime_proposal(runtime)
    if excluded:
        params["impact_decisions"] = [
            {
                "requirement_id": "REQ-001",
                "task_id": tid,
                "disposition": "excluded",
                "exclusion_reason": "Legacy task belongs to an independently maintained release; followup covers current requirement",
            }
        ]
    p = rc.preview(runtime.store.db, runtime.project_root, params)
    c = rc.propose(
        runtime.store.db,
        runtime.project_root,
        dict(params, preview_digest=p["preview_digest"], request_id="HOST-PROP"),
    )
    if excluded:
        rc.apply(
            runtime.store.db,
            runtime.project_root,
            {"change_id": c["change_id"], "expected_version": 0, "request_id": "HOST-APPLY"},
        )
    runner = Runner()
    result = runtime.execute(tid, "HOST-EXEC", runner=runner)
    assert runner.calls > 0
    assert result["state"] == "verifying"


def test_cli_preview_propose_apply_use_service_selection_contract(tmp_path):
    from test_project_journey import prepared
    from test_project_journey import workspace as jw

    from agent_dev.adapters.command_line import dispatch, parser

    ws = jw.__wrapped__(tmp_path)
    root, _repo, _, _ = ws
    _, old = prepared(ws)
    studio = StudioStore(root)
    with studio.connect() as conn:
        reqs = json.loads(
            conn.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    reqs[0]["title"] += " changed"
    _add_studio_revision(studio, "STUDIO-001", reqs)

    def command(*args):
        parsed = parser().parse_args(
            [
                "changes",
                *args,
                "--root",
                str(root),
                "--actor",
                "reviewer",
                "--request-id",
                "CLI-" + args[0],
            ]
        )
        return dispatch(parsed)

    preview = command("preview", "P1", old["import_id"], "CLI actual selection")
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps(preview["selection"]))
    proposal = command(
        "propose",
        "P1",
        old["import_id"],
        preview["preview_digest"],
        "CLI actual selection",
        "--selection",
        str(selection),
    )
    result = command("apply", proposal["change_id"], "0", "--project-id", "P1")
    assert result["state"] == "applied"
    assert (
        command("detail", proposal["change_id"], "--project-id", "P1")["change"]["target_import_id"]
        == result["target_import_id"]
    )


@pytest.mark.parametrize("active", [False, True])
def test_active_lease_cancel_release_repreview_apply_keeps_immutable_proposal(runtime, active):
    from agent_dev.domain.status import TaskStatus
    from agent_dev.domain.transitions import TransitionContext

    tid, params = runtime_proposal(runtime)
    p = rc.preview(runtime.store.db, runtime.project_root, params)
    c = rc.propose(
        runtime.store.db,
        runtime.project_root,
        dict(params, preview_digest=p["preview_digest"], request_id="CANCEL-PROP"),
    )
    before = runtime.store.db.rows(
        "SELECT proposal_json FROM requirement_changes WHERE id=?", (c["change_id"],)
    )[0]
    lease = runtime.safety.acquire("task:" + tid, "own-test")
    try:
        if active:
            task = runtime.store.task(tid)
            runtime.store.transition(
                tid,
                TaskStatus.RUNNING,
                TransitionContext(expected_version=task.version, preflight_passed=True),
                lease,
            )
        with pytest.raises(DomainError, match="lease"):
            rc.apply(
                runtime.store.db,
                runtime.project_root,
                {"change_id": c["change_id"], "expected_version": 0, "request_id": "ACTIVE-APPLY"},
            )
        task = runtime.store.task(tid)
        runtime.store.transition(
            tid, TaskStatus.CANCELLED, TransitionContext(expected_version=task.version), lease
        )
    finally:
        runtime.safety.release(lease)
    # The old immutable proposal must not absorb cancellation/version changes.
    with pytest.raises(DomainError, match="새 미리보기"):
        rc.apply(
            runtime.store.db,
            runtime.project_root,
            {"change_id": c["change_id"], "expected_version": 0, "request_id": "OLD-APPLY"},
        )
    rc.reject(
        runtime.store.db,
        {
            "change_id": c["change_id"],
            "expected_version": 0,
            "reason": "Normal cancellation completed; superseded by fresh preview",
            "actor": "reviewer",
        },
    )
    fresh = rc.preview(runtime.store.db, runtime.project_root, params)
    new = rc.propose(
        runtime.store.db,
        runtime.project_root,
        dict(params, preview_digest=fresh["preview_digest"], request_id="FRESH-PROP"),
    )
    assert (
        rc.apply(
            runtime.store.db,
            runtime.project_root,
            {"change_id": new["change_id"], "expected_version": 0, "request_id": "FRESH-APPLY"},
        )["state"]
        == "applied"
    )
    assert (
        runtime.store.db.rows(
            "SELECT proposal_json FROM requirement_changes WHERE id=?", (c["change_id"],)
        )[0]
        == before
    )
    assert runtime.store.task(tid).state == TaskStatus.CANCELLED


def test_sequential_change_retires_inherited_unchanged_proof_and_impacts_real_original_task(
    tmp_path,
):
    from test_project_journey import actual_run, prepared
    from test_project_journey import workspace as jw

    ws = jw.__wrapped__(tmp_path)
    root, repo, _, target = ws
    first, old = prepared(ws)
    run, tid = actual_run(ws, first)
    link = traceability.create_link(
        repo.db,
        {
            "import_id": old["import_id"],
            "requirement_id": "REQ-001",
            "kind": "test",
            "target_ref": target,
            "task_id": tid,
            "module_id": "backend",
            "criteria_ids": ["REQ-001:AC-01"],
            "verification_run_id": run,
            "request_id": "CHAIN-PROOF",
            "expected_version": 0,
        },
    )
    studio = StudioStore(root)
    with studio.connect() as conn:
        reqs = json.loads(
            conn.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    reqs[1]["description"] += " backend changed"
    _add_studio_revision(studio, "STUDIO-001", reqs)
    params = {
        "project_id": "P1",
        "source_import_id": old["import_id"],
        "reason": "backend revision",
        "actor": "reviewer",
    }
    p = rc.preview(repo.db, root, params)
    c = rc.propose(
        repo.db, root, dict(params, preview_digest=p["preview_digest"], request_id="CHAIN-ONE")
    )
    one = rc.apply(
        repo.db,
        root,
        {"change_id": c["change_id"], "expected_version": 0, "request_id": "CHAIN-ONE-APPLY"},
    )
    reqs[0]["title"] += " now changed"
    _add_studio_revision(studio, "STUDIO-001", reqs)
    params["source_import_id"] = one["target_import_id"]
    params["reason"] = "inherited requirement changed"
    p = rc.preview(repo.db, root, params)
    assert p["impacts"][0]["task_id"] == tid
    c = rc.propose(
        repo.db, root, dict(params, preview_digest=p["preview_digest"], request_id="CHAIN-TWO")
    )
    rc.apply(
        repo.db,
        root,
        {"change_id": c["change_id"], "expected_version": 0, "request_id": "CHAIN-TWO-APPLY"},
    )
    sh._studio_db_path(root).unlink()
    result = traceability.query(repo.db, {"project_id": "P1"})
    assert result["coverage"]["verified"] == 0 and result["coverage"]["total"] == 3
    assert next(l for l in result["links"] if l["id"] == link["id"])["state"] == "historical"
    assert rc.execution_guard(repo.db, tid)["blocked"]


def test_followup_only_becomes_current_proof_after_actual_three_phase_host_and_pytest(tmp_path):
    from test_project_journey import prepared
    from test_project_journey import workspace as jw

    from agent_dev.adapters.workflow_runtime import WorkflowRuntime
    from agent_dev.domain.contracts import PhaseResult

    ws = jw.__wrapped__(tmp_path)
    root, repo, _, target = ws
    config = json.loads(
        repo.db.rows("SELECT content_json FROM project_profile_versions")[0]["content_json"]
    )
    config["phases"] = [
        {"id": p, "capabilities": ["code.read"]} for p in ["planner", "developer", "qa"]
    ]
    repo.revise_profile("P1", config, 0)
    (root / "config").mkdir()
    (root / "config/runtime.json").write_text(
        json.dumps({"orchestrator_v2": {"execute": True, "publish": False}})
    )
    _, old = prepared(ws)
    studio = StudioStore(root)
    with studio.connect() as conn:
        reqs = json.loads(
            conn.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    reqs[0]["title"] += " changed"
    _add_studio_revision(studio, "STUDIO-001", reqs)
    params = {
        "project_id": "P1",
        "source_import_id": old["import_id"],
        "reason": "current proof",
        "actor": "reviewer",
    }
    first = rc.preview(repo.db, root, params)
    tasks = first["handoff"]["selection"]["tasks"]
    criteria = ["REQ-001:AC-01", "REQ-001:AC-02"]
    tasks[0]["verification_mapping"] = [
        {
            "requirement_id": "REQ-001",
            "criteria_ids": criteria,
            "gate": "acceptance",
            "target": target,
        }
    ]
    params["handoff"] = dict(first["handoff"]["selection"], tasks=tasks)
    p = rc.preview(repo.db, root, params)
    # AC membership is a set, while task dependency order remains intact.
    params["handoff"]["tasks"][0]["verification_mapping"][0]["criteria_ids"] = list(
        reversed(criteria)
    )
    semantic = rc.preview(repo.db, root, params)
    assert semantic["preview_digest"] == p["preview_digest"]
    c = rc.propose(
        repo.db,
        root,
        dict(params, preview_digest=semantic["preview_digest"], request_id="CURRENT-PROP"),
    )
    applied = rc.apply(
        repo.db,
        root,
        {"change_id": c["change_id"], "expected_version": 0, "request_id": "CURRENT-APPLY"},
    )
    assert traceability.coverage(repo.db, applied["target_import_id"])["verified"] == 0
    repo.approve("design", applied["design_id"], 1, "reviewer")
    repo.approve("plan", applied["plan_id"], 1, "reviewer")
    rt = WorkflowRuntime(root)
    tid = tasks[0]["id"]
    rt.store.create_snapshot(tid, rt.resolve_snapshot(tid))

    class LocalRunner:
        calls = 0

        def execute(self, request):
            self.calls += 1
            return PhaseResult('```json\n{"handoff_status":"pass"}\n```')

    runner = LocalRunner()
    assert rt.execute(tid, "CURRENT-HOST", runner=runner)["state"] == "verifying"
    assert runner.calls == 3
    actual = rt.execute(tid, "CURRENT-PYTEST", verify=True)
    assert actual["passed"]
    link = traceability.create_link(
        repo.db,
        {
            "import_id": applied["target_import_id"],
            "requirement_id": "REQ-001",
            "kind": "test",
            "target_ref": target,
            "task_id": tid,
            "module_id": "backend",
            "criteria_ids": criteria,
            "verification_run_id": actual["run_id"],
            "request_id": "CURRENT-LINK",
            "expected_version": 0,
        },
    )
    assert link["state"] == "verified"
    assert traceability.coverage(repo.db, applied["target_import_id"])["verified"] == 2
    assert rc.detail(repo.db, c["change_id"])["impacts"][0]["completion"] == "revalidated"


@pytest.mark.parametrize("mutation", ["bytes", "same_bytes_new_commit"])
def test_apply_binds_actual_linked_source_evidence_even_when_old_link_already_stale(
    tmp_path, mutation
):
    import subprocess

    from test_project_journey import actual_run, prepared
    from test_project_journey import workspace as jw

    ws = jw.__wrapped__(tmp_path)
    root, repo, source, target = ws
    first, old = prepared(ws)
    run, tid = actual_run(ws, first)
    traceability.create_link(
        repo.db,
        {
            "import_id": old["import_id"],
            "requirement_id": "REQ-001",
            "kind": "test",
            "target_ref": target,
            "task_id": tid,
            "module_id": "backend",
            "criteria_ids": ["REQ-001:AC-01"],
            "verification_run_id": run,
            "request_id": "STALE-LINK",
            "expected_version": 0,
        },
    )
    args = proposed((root, repo, StudioStore(root), first, old))
    # Studio revision changes already mark the old link stale. Source bytes and
    # commit identity must still be bound independently, not only its state label.
    if mutation == "bytes":
        (source / "tests/test_acceptance.py").write_text(
            "def test_acceptance():\n    assert False\n"
        )
    else:
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
                "new identity same bytes",
            ],
            check=True,
        )
    with pytest.raises(DomainError, match="새 미리보기"):
        apply(args)


def test_complete_scoped_selection_pagination_reaches_older_project_and_import(imported):
    from agent_dev.adapters.state.sqlite.database import Database

    root, repo, studio, _, old = imported
    config = json.loads(
        repo.db.rows("SELECT content_json FROM project_profile_versions")[0]["content_json"]
    )
    repo.register_project("P0", "Older project", config)
    repo.register_project("P2", "Another project", config)
    with studio.connect() as conn:
        reqs = json.loads(
            conn.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    reqs[0]["title"] += " current revision"
    _add_studio_revision(studio, "STUDIO-001", reqs)
    p = sh.preview(repo.db, root, {"project_id": "P1", "source_studio_id": "STUDIO-001"})
    newest = sh.commit(
        repo.db,
        root,
        dict(p["selection"], preview_digest=p["preview_digest"], request_id="PAGE-IMPORT"),
    )
    ro = Database(repo.db.path, read_only=True)
    tables = ["projects", "handoff_imports", "commands", "events"]
    before = {t: repo.db.rows("SELECT * FROM " + t) for t in tables}
    page0 = rc.bootstrap(ro, {"project_id": "P1", "limit": 1, "offset": 0}, root)
    page1 = rc.bootstrap(ro, {"project_id": "P1", "limit": 1, "offset": 1}, root)
    assert page0["imports"][0]["id"] == newest["import_id"]
    assert page0["imports_next_offset"] == 1
    assert page1["imports"][0]["id"] == old["import_id"]
    assert page0["projects"][0]["id"] == "P0" and page1["projects"][0]["id"] == "P1"
    assert before == {t: repo.db.rows("SELECT * FROM " + t) for t in tables}


def test_new_source_must_use_latest_finalized_spec_and_mock_not_stale_project_pointer(imported):
    root, repo, studio, _, old = imported
    with studio.connect() as conn:
        reqs = json.loads(
            conn.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0]
        )["requirements"]
    reqs[0]["title"] += " latest approved revision"
    _add_studio_revision(studio, "STUDIO-001", reqs)
    with studio.transaction() as conn:
        conn.execute(
            "UPDATE projects SET spec_revision=1,mock_revision=1,version=version+1 WHERE id='STUDIO-001'"
        )
    with pytest.raises(DomainError, match="최신"):
        rc.preview(
            repo.db,
            root,
            {
                "project_id": "P1",
                "source_import_id": old["import_id"],
                "new_studio_id": "STUDIO-001",
                "reason": "current pointer mismatch",
                "actor": "reviewer",
            },
        )
