"""Digest-bound requirement revisions; proposals never mutate current execution."""

import contextlib
import json
import re
import time
from typing import Any, NoReturn
from urllib.parse import urlencode

from agent_dev.adapters import studio_handoff as sh
from agent_dev.adapters.state.sqlite.store import WorkflowRepository, identifier
from agent_dev.domain.content import canonical, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode


def fail(message, code=ErrorCode.INVALID_INPUT) -> NoReturn:
    raise DomainError(code, message)


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        fail("id: 영문·숫자·밑줄·하이픈 1~100자를 사용해주세요.")
    return value


def _secret_values(db):
    from agent_dev.adapters.secrets import registered_secrets

    return tuple(registered_secrets(db.path.parent))


def _safe(value: Any, secrets: tuple[str, ...], field: str = "") -> Any:
    """Redact descriptive content; reject secrets in operational identities and paths."""
    text_fields = {
        "title", "name", "brief", "description", "source_quote", "acceptance",
        "reason", "actor", "exclusion_reason", "unchanged_followup_reason",
        "spec_source", "notes", "note", "comment", "summary", "explanation", "error",
    }
    if isinstance(value, dict):
        if any(redact(str(k), secrets) != str(k) for k in value):
            fail("등록된 비밀 값이 데이터 필드 이름에 포함되어 있습니다.")
        return {k: _safe(v, secrets, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v, secrets, field) for v in value]
    if isinstance(value, str):
        if field in {"proposal_json", "definition_json", "content_json", "payload_json"}:
            return canonical(_safe(json.loads(value), secrets))
        safe = redact(value, secrets)
        if safe != value and field not in text_fields:
            fail("등록된 비밀 값이 식별자·경로·실행 설정에 포함되어 있습니다. 해당 값을 제거해주세요.")
        return safe
    return value


def _prepare_handoff(conn, bundle, params, secrets, **options) -> dict:
    # Keep source digests and executable selection exact; only descriptive REQ content changes.
    safe_bundle = dict(bundle, requirements=_safe(bundle["requirements"], secrets))
    _safe(bundle["source"], secrets)
    project = params.get("project_id")
    profile = conn.execute(
        "SELECT content_json FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
        (project,),
    ).fetchone()
    if profile:
        _safe(json.loads(profile[0]), secrets)
    return _safe(sh._prepare(conn, safe_bundle, _safe(params, secrets), **options), secrets)


def requirement_delta(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    return {
        key: "added"
        if key not in before
        else "removed"
        if key not in after
        else "unchanged"
        if before[key] == after[key]
        else "changed"
        for key in sorted(before.keys() | after.keys())
    }


def _project(conn, project_id):
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        fail("project_id: 등록된 프로젝트를 선택해주세요.")
    return dict(row)


def _load_import(conn, import_id, project_id):
    _project(conn, project_id)
    row = conn.execute("SELECT * FROM handoff_imports WHERE id=?", (import_id,)).fetchone()
    if row is None:
        fail("source_import_id: 등록된 인계를 선택해주세요.")
    if row["project_id"] != project_id:
        fail("source_import_id: 프로젝트 범위를 벗어난 인계입니다.", ErrorCode.POLICY_VIOLATION)
    return dict(row)


def _change(conn, params):
    row = conn.execute(
        "SELECT * FROM requirement_changes WHERE id=?", (params.get("change_id"),)
    ).fetchone()
    if row is None:
        fail("change_id: 변경 요청을 찾을 수 없습니다.")
    _project(conn, row["project_id"])
    if params.get("project_id") and params["project_id"] != row["project_id"]:
        fail("project_id: 변경 요청의 프로젝트와 다릅니다.", ErrorCode.POLICY_VIOLATION)
    return dict(row)


def _read_new_bundle(workspace_root, studio_id):
    with contextlib.closing(sh._open(sh._studio_db_path(workspace_root))) as studio:
        studio.execute("BEGIN")
        return sh._read_bundle(studio, studio_id)


def _evidence(conn, db, import_id, req_id, task_id):
    from agent_dev.adapters import traceability as tr

    rows = conn.execute(
        "SELECT * FROM requirement_links WHERE import_id=? AND requirement_id=? ORDER BY id",
        (import_id, req_id),
    ).fetchall()
    evidence = []
    for row in rows:
        if row["task_id"] not in (None, task_id):
            continue
        item = dict(row)
        evaluated = tr._evaluate(conn, db, row)
        item["current_state"] = evaluated["state"]
        if row["kind"] in {"file", "test", "commit"}:
            try:
                context = tr._context(
                    conn, dict(row, criteria_ids=json.loads(row["criteria_json"]))
                )
                from agent_dev.adapters.worktrees import commit_fingerprint, evidence_digest

                item["current_digest"] = tr._mapped_digest(dict(row), context[-1])
                item["current_proof"] = tr._proof(
                    conn, db, dict(row, criteria_ids=json.loads(row["criteria_json"])), context
                )[0]
                if context[3]:
                    item["source_bytes"] = evidence_digest(context[3].get("repositories", {}))
                    item["source_commits"] = commit_fingerprint(context[3].get("repositories", {}))
            except (DomainError, OSError, ValueError, KeyError):
                item["current_digest"] = None
        evidence.append(item)
    return evidence


def _impacts(conn, db, imported, delta, decisions):
    if not isinstance(decisions, list):
        fail("impact_decisions: 배열이 필요합니다。")
    impacts = []
    consumed = set()
    origin_cache = {}
    from agent_dev.adapters.traceability import _requirement_origin

    for req_id, disposition in delta.items():
        if disposition not in {"changed", "removed"}:
            continue
        origin_id = _requirement_origin(conn, imported["id"], req_id, origin_cache)
        origin = _load_import(conn, origin_id, imported["project_id"])
        linked = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT task_id FROM requirement_links WHERE import_id=? AND requirement_id=? AND task_id IS NOT NULL ORDER BY task_id",
                (origin_id, req_id),
            )
        ]
        manual = [
            d.get("task_id")
            for d in decisions
            if isinstance(d, dict) and d.get("requirement_id") == req_id and d.get("task_id")
        ]
        targets = sorted(set(linked + manual)) or [None]
        for tid in targets:
            selected = [
                (i, d)
                for i, d in enumerate(decisions)
                if isinstance(d, dict)
                and d.get("requirement_id") == req_id
                and d.get("task_id") == tid
            ]
            if len(selected) > 1:
                fail("impact_decisions: 중복 영향 판단입니다.")
            decision = selected[0][1] if selected else {}
            if selected:
                consumed.add(selected[0][0])
            task = (
                conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone() if tid else None
            )
            if tid:
                if not task or task["project_id"] != imported["project_id"]:
                    fail(
                        "task_id: 프로젝트 범위를 벗어난 영향 작업입니다.",
                        ErrorCode.POLICY_VIOLATION,
                    )
                definition = json.loads(task["definition_json"])
                if tid not in linked and not (
                    req_id in definition.get("requirement_ids", [])
                    and task["plan_id"] == origin["plan_id"]
                ):
                    fail(
                        "task_id: 요구사항과 연결된 실제 작업을 선택해주세요.",
                        ErrorCode.POLICY_VIOLATION,
                    )
            evidence = _evidence(conn, db, origin_id, req_id, tid)
            ed = digest(evidence)
            if "evidence_digest" in decision and decision["evidence_digest"] != ed:
                fail("evidence_digest: 서버 근거와 다릅니다.", ErrorCode.STALE_VERSION)
            state = decision.get("disposition", disposition if tid in linked else "unknown")
            reason = decision.get("exclusion_reason", "").strip()
            if (
                state not in {disposition, "unknown", "excluded"}
                or (state == "excluded" and not reason)
                or (not tid and state != "excluded" and state != "unknown")
            ):
                fail("impact_decisions: 실제 매핑 또는 제외 이유가 필요합니다.")
            lease = (
                conn.execute("SELECT * FROM leases WHERE resource=?", ("task:" + tid,)).fetchone()
                if tid
                else None
            )
            inferred = []
            if tid:
                inferred = [
                    {"task_id": r[0], "reason": "작업 의존 관계에서 추론", "inferred": True}
                    for r in conn.execute(
                        "SELECT task_id FROM task_dependencies WHERE depends_on_task_id=? UNION SELECT depends_on_task_id FROM task_dependencies WHERE task_id=? LIMIT 200",
                        (tid, tid),
                    )
                ]
            impacts.append(
                {
                    "evidence_import_id": origin_id,
                    "inferred_candidates": inferred,
                    "task_id": tid,
                    "requirement_id": req_id,
                    "task_version": task["version"] if task else None,
                    "task_state": task["state"] if task else None,
                    "task_digest": digest(dict(task)) if task else None,
                    "lease": dict(lease) if lease and lease["expires_at"] > time.time() else None,
                    "evidence": evidence,
                    "evidence_digest": ed,
                    "disposition": state,
                    "exclusion_reason": reason,
                    "followup_task_id": None,
                }
            )
    if consumed != set(range(len(decisions))):
        fail("impact_decisions: 현재 변경 요구사항·작업에 없는 판단입니다.")
    return impacts


def _prepare_preview(conn, db, bundle, params) -> dict:
    secret_values = _secret_values(db)
    params = _safe(params, secret_values)
    project_id = params.get("project_id")
    imported = _load_import(conn, params.get("source_import_id"), project_id)
    if (
        params.get("new_studio_id", imported["source_studio_id"]) != imported["source_studio_id"]
        or bundle["project"]["id"] != imported["source_studio_id"]
    ):
        fail(
            "new_studio_id: 같은 Studio 프로젝트의 새 버전만 사용할 수 있습니다.",
            ErrorCode.POLICY_VIOLATION,
        )
    reason, actor = params.get("reason", "").strip(), params.get("actor", "").strip()
    if not reason or not actor:
        fail("reason·actor: 변경 이유와 작성자가 필요합니다.")
    reason, actor = redact(reason, secret_values), redact(actor, secret_values)
    before = {
        r["requirement_id"]: r["content_digest"]
        for r in conn.execute(
            "SELECT * FROM imported_requirements WHERE import_id=? ORDER BY requirement_id",
            (imported["id"],),
        )
    }
    after = {
        r["id"]: digest(sh.normalized_requirement(r, secret_values)) for r in bundle["requirements"]
    }
    delta = requirement_delta(before, after)
    decisions = params.get("impact_decisions", [])
    impacts = _impacts(conn, db, imported, delta, decisions)
    handoff = dict(
        params.get("handoff", {}),
        project_id=project_id,
        source_studio_id=imported["source_studio_id"],
    )
    needed = {r for r, d in delta.items() if d in {"added", "changed"}}
    if "tasks" not in handoff:
        default = _prepare_handoff(conn, bundle, handoff, secret_values, enforce_existing_intent=False)
        tasks = [
            t for t in default["selection"]["tasks"] if needed.intersection(t["requirement_ids"])
        ]
        # Removal-only revisions require a deliberately selected cleanup task in the current scope.
        if not tasks:
            tasks = default["selection"]["tasks"]
        handoff["tasks"] = tasks
    prepared = _prepare_handoff(
        conn, bundle, handoff, secret_values, required_requirement_ids=needed, enforce_existing_intent=False
    )
    tasks = prepared["selection"]["tasks"]
    extra = {r for t in tasks for r in t["requirement_ids"] if delta[r] == "unchanged"}
    justification = params.get("unchanged_followup_reason", "").strip()
    if extra and not justification and any(d != "unchanged" for d in delta.values()):
        fail("unchanged_followup_reason: 변경 없는 요구사항의 추가 작업 이유가 필요합니다.")
    followups = params.get("followup_mapping", [])
    if not isinstance(followups, list):
        fail("followup_mapping: 배열이 필요합니다.")
    used = set()
    for impact in impacts:
        matches = [
            (i, m)
            for i, m in enumerate(followups)
            if isinstance(m, dict)
            and m.get("requirement_id") == impact["requirement_id"]
            and m.get("task_id") == impact["task_id"]
        ]
        if len(matches) > 1:
            fail("followup_mapping: 중복 매핑입니다.")
        candidates = [t["id"] for t in tasks if impact["requirement_id"] in t["requirement_ids"]]
        if matches:
            i, m = matches[0]
            used.add(i)
            target = m.get("followup_task_id")
            if target not in {t["id"] for t in tasks} or (
                delta[impact["requirement_id"]] != "removed"
                and not any(
                    t["id"] == target and impact["requirement_id"] in t["requirement_ids"]
                    for t in tasks
                )
            ):
                fail("followup_task_id: 검증된 새 계획의 작업을 선택해주세요.")
        else:
            target = candidates[0] if len(candidates) == 1 else None
        impact["planned_followup_task_id"] = target
    if used != set(range(len(followups))):
        fail("followup_mapping: 영향 대상에 없는 매핑입니다.")
    intent = {
        "project_id": project_id,
        "source_import_id": imported["id"],
        "new_studio_id": imported["source_studio_id"],
        "reason": reason,
        "actor": actor,
        "handoff": prepared["selection"],
        "impact_decisions": decisions,
        "followup_mapping": followups,
        "unchanged_followup_reason": justification,
    }
    latest_import = conn.execute(
        "SELECT id FROM handoff_imports WHERE project_id=? AND source_studio_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (project_id, imported["source_studio_id"]),
    ).fetchone()[0]
    proof = {
        "current_import_id": latest_import,
        "intent": intent,
        "source": _safe(bundle["source"], secret_values),
        "source_bundle_digest": bundle["bundle_digest"],
        "profile_digest": prepared["profile_digest"],
        "handoff_digest": prepared["preview_digest"],
        "before_digests": before,
        "after_digests": after,
        "delta": delta,
        "impacts": impacts,
    }
    proof = _safe(proof, secret_values)
    return dict(
        proof,
        project_id=project_id,
        source_import_id=imported["id"],
        preview_digest=digest(proof),
        source_spec_digest=bundle["spec"]["digest"],
        source_mock_digest=bundle["mock"]["digest"],
        selection=intent,
        handoff=prepared,
    )


def preview(canonical_db, workspace_root, params):
    _safe(params, _secret_values(canonical_db))
    with contextlib.closing(canonical_db.connect()) as conn:
        conn.execute("BEGIN")
        imported = _load_import(conn, params.get("source_import_id"), params.get("project_id"))
        bundle = _read_new_bundle(
            workspace_root, params.get("new_studio_id", imported["source_studio_id"])
        )
        return _prepare_preview(conn, canonical_db, bundle, params)


def _retry(conn, key, payload_digest):
    row = conn.execute("SELECT * FROM commands WHERE key=?", (key,)).fetchone()
    if row:
        if row["request_digest"] != payload_digest:
            fail("request_id: 다른 입력에 사용된 키입니다.", ErrorCode.STALE_VERSION)
        return json.loads(row["result_json"])
    return None


def _command(conn, key, payload_digest, result):
    conn.execute(
        "INSERT INTO commands VALUES(?,?,'done',?,?)",
        (key, payload_digest, canonical(result), time.time()),
    )


def propose(canonical_db, workspace_root, params):
    _safe(params, _secret_values(canonical_db))
    key, pd = "changes:" + _id(params.get("request_id")), digest(params)
    with canonical_db.transaction() as conn:
        cached = _retry(conn, key, pd)
        if cached is not None:
            return cached
        imported = _load_import(conn, params.get("source_import_id"), params.get("project_id"))
        bundle = _read_new_bundle(
            workspace_root, params.get("new_studio_id", imported["source_studio_id"])
        )
        current = _prepare_preview(conn, canonical_db, bundle, params)
        if current["preview_digest"] != params.get("preview_digest"):
            fail(
                "미리보기 이후 원본·대상·프로파일·근거·입력이 변경되었습니다. 새 미리보기가 필요합니다.",
                ErrorCode.STALE_VERSION,
            )
        cid = identifier()
        conn.execute(
            "INSERT INTO requirement_changes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'proposed',0,?,?,NULL)",
            (
                cid,
                params["project_id"],
                imported["id"],
                None,
                imported["source_studio_id"],
                current["source_spec_digest"],
                current["source_mock_digest"],
                current["source_bundle_digest"],
                canonical(current),
                digest(current),
                current["intent"]["reason"],
                current["intent"]["actor"],
                key,
                time.time(),
            ),
        )
        for i in current["impacts"]:
            if i["task_id"] is None:
                continue  # NOT NULL schema: unresolved impacts remain in immutable proposal JSON.
            conn.execute(
                "INSERT INTO requirement_change_impacts VALUES(?,?,?,?,?,?,?,?)",
                (
                    cid,
                    i["task_id"],
                    i["requirement_id"],
                    i["task_version"],
                    i["evidence_digest"],
                    i["disposition"],
                    i["exclusion_reason"],
                    None,
                ),
            )
        WorkflowRepository(canonical_db)._event(
            conn,
            params["project_id"],
            "RequirementChangeProposed",
            {
                "change_id": cid,
                "reason": current["intent"]["reason"],
                "actor": current["intent"]["actor"],
                "delta_summary": current["delta"],
            },
        )
        result = {
            "change_id": cid,
            "project_id": params["project_id"],
            "state": "proposed",
            "version": 0,
        }
        _command(conn, key, pd, result)
        return result


def apply(canonical_db, workspace_root, params):
    _safe(params, _secret_values(canonical_db))
    if type(params.get("expected_version")) is not int:
        fail("expected_version: 정수가 필요합니다.")
    key, pd = "apply_change:" + _id(params.get("request_id")), digest(params)
    studio = None
    try:
        with canonical_db.transaction() as conn:
            cached = _retry(conn, key, pd)
            if cached is not None:
                return cached  # Exact retry never needs the external Studio.
            change = _change(conn, params)
            if change["version"] != params["expected_version"] or change["state"] != "proposed":
                fail("expected_version: 제안 상태·버전이 변경되었습니다.", ErrorCode.STALE_VERSION)
            proposal = json.loads(change["proposal_json"])
            if proposal["current_import_id"] != change["source_import_id"]:
                fail(
                    "source_import_id: 현재 개정의 인계를 선택해 다시 미리보기해주세요.",
                    ErrorCode.STALE_VERSION,
                )
            for impact in proposal["impacts"]:
                if impact["disposition"] == "unknown":
                    fail(
                        "미확정 영향이 있습니다. 실제 작업 매핑 또는 이유가 있는 제외 판단으로 다시 제안해주세요.",
                        ErrorCode.APPROVAL_REQUIRED,
                    )
                if impact["disposition"] == "excluded":
                    continue
                tid = impact["task_id"]
                task = conn.execute("SELECT state FROM tasks WHERE id=?", (tid,)).fetchone()
                lease = conn.execute(
                    "SELECT expires_at FROM leases WHERE resource=?", ("task:" + tid,)
                ).fetchone()
                if task["state"] in {"running", "verifying", "waiting_for_capacity"} or (
                    lease and lease["expires_at"] > time.time()
                ):
                    fail(
                        f"영향 작업 {tid}의 실행/lease가 활성 상태입니다. 기존 취소 후 lease 해제·만료를 기다리고 새 미리보기를 진행해주세요.",
                        ErrorCode.LEASE_CONFLICT,
                    )
                if not impact["planned_followup_task_id"]:
                    fail(
                        "후속 작업 매핑이 필요합니다. 새 계획의 작업을 선택해 다시 제안해주세요.",
                        ErrorCode.APPROVAL_REQUIRED,
                    )
            studio = sh._open(sh._studio_db_path(workspace_root), writable=True)
            studio.execute("BEGIN IMMEDIATE")
            bundle = sh._read_bundle(studio, change["source_studio_id"])
            current = _prepare_preview(conn, canonical_db, bundle, proposal["intent"])
            if current["preview_digest"] != proposal["preview_digest"]:
                fail(
                    "미리보기 이후 원본·대상·프로파일·작업·근거가 변경되었습니다. 새 미리보기가 필요합니다.",
                    ErrorCode.STALE_VERSION,
                )
            existing = conn.execute(
                "SELECT id FROM handoff_imports WHERE project_id=? AND source_studio_id=? AND spec_digest=? AND mock_digest=?",
                (
                    change["project_id"],
                    change["source_studio_id"],
                    change["source_spec_digest"],
                    change["source_mock_digest"],
                ),
            ).fetchone()
            if existing:
                fail(
                    "이 원본은 이미 인계되었습니다. 기존 변경을 확인하거나 새 원본으로 미리보기해주세요.",
                    ErrorCode.STALE_VERSION,
                )
            written = sh._write_handoff(
                conn, canonical_db, bundle, current["handoff"], "changes:apply:" + change["id"]
            )
            conn.execute(
                "UPDATE requirement_changes SET state='applied',version=version+1,target_import_id=?,applied_at=? WHERE id=?",
                (written["import_id"], time.time(), change["id"]),
            )
            repo = WorkflowRepository(canonical_db)
            repo._event(
                conn,
                change["project_id"],
                "RequirementChangeApplied",
                dict(change_id=change["id"], **written),
            )
            result = {
                "change_id": change["id"],
                "project_id": change["project_id"],
                "target_import_id": written["import_id"],
                "design_id": written["design_id"],
                "plan_id": written["plan_id"],
                "state": "applied",
                "version": 1,
                "next_actions": [
                    {
                        "href": "/agent?"
                        + urlencode(
                            {
                                "operation": "design.approve",
                                "design_id": written["design_id"],
                                "version": 1,
                            }
                        ),
                        "label": "1. 새 설계 승인",
                    },
                    {
                        "href": "/agent?"
                        + urlencode({"operation": "plan.approve", "plan_id": written["plan_id"]}),
                        "label": "2. 설계 승인 후 새 계획 승인",
                    },
                    {
                        "href": "/traceability?"
                        + urlencode(
                            {"project_id": change["project_id"], "import_id": written["import_id"]}
                        ),
                        "label": "후속 작업과 실제 검증 근거",
                    },
                ],
            }
            _command(conn, key, pd, result)
        return result
    finally:
        if studio is not None:
            studio.rollback()
            studio.close()


def reject(canonical_db, params):
    params = _safe(params, _secret_values(canonical_db))
    if (
        type(params.get("expected_version")) is not int
        or not params.get("reason", "").strip()
        or not params.get("actor", "").strip()
    ):
        fail("expected_version·reason·actor: 버전·거부 이유·작성자가 필요합니다.")
    with canonical_db.transaction() as conn:
        change = _change(conn, params)
        if change["version"] != params["expected_version"] or change["state"] != "proposed":
            fail("expected_version: 제안 상태·버전이 변경되었습니다.", ErrorCode.STALE_VERSION)
        conn.execute(
            "UPDATE requirement_changes SET state='rejected',version=version+1 WHERE id=?",
            (change["id"],),
        )
        WorkflowRepository(canonical_db)._event(
            conn,
            change["project_id"],
            "RequirementChangeRejected",
            {
                "change_id": change["id"],
                "reason": params["reason"].strip(),
                "actor": params["actor"].strip(),
            },
        )
        return {"change_id": change["id"], "state": "rejected", "version": change["version"] + 1}


def guard_in_transaction(conn, task_id, *, enforce=False):
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT i.*,c.target_import_id,h.design_id,h.plan_id FROM requirement_change_impacts i JOIN requirement_changes c ON c.id=i.change_id LEFT JOIN handoff_imports h ON h.id=c.target_import_id WHERE i.task_id=? AND c.state='applied' AND i.disposition IN ('changed','removed','unknown')",
            (task_id,),
        )
    ]
    actions = [
        {
            "href": "/changes?" + urlencode({"change_id": r["change_id"]}),
            "label": "새 설계·계획 및 후속 작업 확인",
            "followup_task_id": r["followup_task_id"],
            "design_id": r["design_id"],
            "plan_id": r["plan_id"],
        }
        for r in rows
    ]
    result = {
        "blocked": bool(rows),
        "task_id": task_id,
        "change_impacts": rows,
        "next_actions": actions,
    }
    if rows and enforce:
        fail(
            "적용된 요구사항 변경의 이전 작업입니다. 새 설계·계획을 승인하고 후속 작업에서 검증해주세요. "
            + canonical(actions),
            ErrorCode.APPROVAL_REQUIRED,
        )
    return result


def execution_guard(canonical_db, task_id, *, enforce=False):
    _safe({"task_id": task_id}, _secret_values(canonical_db))
    with contextlib.closing(canonical_db.connect()) as conn:
        conn.execute("BEGIN")
        return guard_in_transaction(conn, task_id, enforce=enforce)


def _pagination(params):
    try:
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
    except (ValueError, TypeError):
        fail("limit·offset: 정수가 필요합니다.")
    if not 1 <= limit <= 200 or offset < 0:
        fail("limit: 1~200, offset: 0 이상이어야 합니다.")
    return limit, offset


def query(canonical_db, params):
    _safe(params, _secret_values(canonical_db))
    limit, offset = _pagination(params)
    with contextlib.closing(canonical_db.connect()) as conn:
        project = params.get("project_id", "")
        if project:
            _project(conn, project)
        state = params.get("state", "")
        if state and state not in {"proposed", "applied", "rejected"}:
            fail("state: 상태 필터를 확인해주세요.")
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM requirement_changes WHERE (?='' OR project_id=?) AND (?='' OR state=?) ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (project, project, state, state, limit + 1, offset),
            )
        ]
        return _safe({
            "changes": rows[:limit],
            "next_offset": offset + limit if len(rows) > limit else None,
        }, _secret_values(canonical_db))


def detail(canonical_db, change_id, project_id=None):
    _safe({"change_id": change_id, "project_id": project_id}, _secret_values(canonical_db))
    with contextlib.closing(canonical_db.connect()) as conn:
        conn.execute("BEGIN")
        change = _change(conn, {"change_id": change_id, "project_id": project_id})
        proposal = json.loads(change["proposal_json"])
        if change["target_import_id"]:
            target = conn.execute(
                "SELECT design_id,plan_id FROM handoff_imports WHERE id=?",
                (change["target_import_id"],),
            ).fetchone()
            change.update(dict(target))
        impacts = proposal["impacts"]
        persisted = {
            (r["task_id"], r["requirement_id"]): dict(r)
            for r in conn.execute(
                "SELECT * FROM requirement_change_impacts WHERE change_id=?", (change_id,)
            )
        }
        for impact in impacts:
            task = None
            row = persisted.get((impact["task_id"], impact["requirement_id"]))
            if row:
                impact["followup_task_id"] = row["followup_task_id"]
                task = conn.execute(
                    "SELECT state FROM tasks WHERE id=?", (row["followup_task_id"],)
                ).fetchone()
                impact["followup_state"] = task["state"] if task else "awaiting_plan_approval"
            impact["completion"] = (
                "excluded"
                if impact["disposition"] == "excluded"
                else "unresolved"
                if impact["disposition"] == "unknown"
                else "pending_revalidation"
            )
            if impact.get("followup_task_id"):
                from agent_dev.adapters import traceability

                current = conn.execute(
                    "SELECT * FROM requirement_links WHERE task_id=? AND kind='test'",
                    (impact["followup_task_id"],),
                ).fetchall()
                req = conn.execute(
                    "SELECT content_json FROM imported_requirements WHERE import_id=? AND requirement_id=?",
                    (change["target_import_id"], impact["requirement_id"]),
                ).fetchone()
                required = set(json.loads(req[0]).get("criteria_ids", [])) if req else set()
                verified = set()
                for row in current:
                    link = traceability._evaluate(conn, canonical_db, row)
                    if (
                        link["state"] == "verified"
                        and row["requirement_id"] == impact["requirement_id"]
                    ):
                        verified.update(link["criteria_ids"])
                if required and required <= verified:
                    impact["completion"] = "revalidated"
                elif impact["disposition"] == "removed" and task and task["state"] == "completed":
                    impact["completion"] = "removal_followup_completed"
        return _safe({
            "change": change,
            "impacts": impacts,
            "delta": proposal["delta"],
            "selection": proposal["intent"],
        }, _secret_values(canonical_db))


def bootstrap(canonical_db, params, workspace_root=None):
    _safe(params, _secret_values(canonical_db))
    limit, offset = _pagination(params)
    project = params.get("project_id", "")
    secret_values = _secret_values(canonical_db)
    with contextlib.closing(canonical_db.connect()) as conn:
        conn.execute("BEGIN")
        if project:
            _project(conn, project)
        projects = [
            dict(r)
            for r in conn.execute(
                "SELECT id,name,version FROM projects ORDER BY id LIMIT ? OFFSET ?",
                (limit + 1, offset),
            )
        ]
        imports = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM handoff_imports WHERE (?='' OR project_id=?) ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                (project, project, limit + 1, offset),
            )
        ]
        profiles = (
            [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT ? OFFSET ?",
                    (project, limit + 1, offset),
                )
            ]
            if project
            else []
        )
        tasks = (
            [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM tasks WHERE project_id=? ORDER BY id LIMIT ? OFFSET ?",
                    (project, limit + 1, offset),
                )
            ]
            if project
            else []
        )
        import_requirements = [
            dict(r)
            for imp in imports[:limit]
            for r in conn.execute(
                "SELECT * FROM imported_requirements WHERE import_id=? ORDER BY requirement_id",
                (imp["id"],),
            )
        ]
        sources = []
        source_ids = []
        if project:
            source_ids = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT source_studio_id FROM handoff_imports WHERE project_id=? ORDER BY source_studio_id LIMIT ? OFFSET ?",
                    (project, limit + 1, offset),
                )
            ]
            for sid in source_ids[:limit]:
                try:
                    bundle = _read_new_bundle(
                        workspace_root or sh.studio_root_for(canonical_db), sid
                    )
                    prepared = _prepare_handoff(
                        conn, bundle, {"project_id": project}, secret_values, enforce_existing_intent=False
                    )

                    requirements = [
                        sh.normalized_requirement(
                            r, secret_values
                        )
                        for r in bundle["requirements"]
                    ]
                    sources.append(
                        {
                            "id": sid,
                            "finalized": True,
                            "source": _safe(bundle["source"], secret_values),
                            "bundle_digest": bundle["bundle_digest"],
                            "requirements": requirements,
                            "digests": {r["id"]: digest(r) for r in requirements},
                            "handoff": prepared["selection"],
                        }
                    )
                except DomainError as error:
                    sources.append({"id": sid, "finalized": False, "error": str(error)})
        return _safe(dict(
            query(canonical_db, params),
            projects=projects[:limit],
            imports=imports[:limit],
            profiles=profiles[:limit],
            tasks=tasks[:limit],
            import_requirements=import_requirements,
            sources=sources,
            sources_next_offset=offset + limit if project and len(source_ids) > limit else None,
            projects_next_offset=offset + limit if len(projects) > limit else None,
            imports_next_offset=offset + limit if len(imports) > limit else None,
            profiles_next_offset=offset + limit if len(profiles) > limit else None,
            tasks_next_offset=offset + limit if len(tasks) > limit else None,
            project_id=project,
        ), secret_values)
