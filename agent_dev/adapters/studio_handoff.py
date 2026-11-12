"""Digest-bound, draft-only Studio handoff; canonical→Studio writer ownership."""

import contextlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import NoReturn

from agent_dev.adapters.state.sqlite.store import WorkflowRepository, identifier, validate_dag
from agent_dev.domain.content import canonical, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode


def fail(message, code=ErrorCode.INVALID_INPUT) -> NoReturn:
    raise DomainError(code, message)


def studio_root_for(canonical_db):
    """The folder whose Studio DB pairs with a canonical DB (external config or source)."""
    from agent_dev.adapters.workspace_locator import workspace_locations
    from agent_dev.core.storage_locations import EXTERNAL

    folder = Path(canonical_db.path).resolve().parent
    return folder if workspace_locations(folder).layout == EXTERNAL else folder.parent


def _studio_db_path(workspace_root):
    from agent_dev.adapters.workspace_locator import workspace_locations
    from agent_dev.core.storage_locations import EXTERNAL

    root = Path(workspace_root).expanduser().resolve()
    locations = workspace_locations(root)
    candidates = (".agent-dev/studio/studio.db", "studio/studio.db")
    if locations.layout == EXTERNAL:
        root = locations.config_root
        candidates = (locations.studio_db.relative_to(root).as_posix(),)
    for relative in candidates:
        path = root / relative
        if any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(root)):
            fail("Studio 경로에 심볼릭 링크를 사용할 수 없습니다.")
        if path.is_file() and path.resolve().is_relative_to(root):
            return path
    fail("Studio DB를 찾을 수 없습니다.")


def _open(path, writable=False):
    conn = sqlite3.connect(
        path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
        uri=True,
        timeout=10,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
        conn.close()
        fail("지원하지 않는 Studio DB 버전입니다.")
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _read_bundle(conn, studio_id):
    row = conn.execute("SELECT * FROM projects WHERE id=?", (studio_id,)).fetchone()
    if row is None:
        fail("Studio 프로젝트를 찾을 수 없습니다.")
    project = dict(row)
    latest_spec = conn.execute("SELECT MAX(revision) FROM specs WHERE project_id=?", (studio_id,)).fetchone()[0]
    latest_mock = conn.execute("SELECT MAX(revision) FROM mocks WHERE project_id=?", (studio_id,)).fetchone()[0]
    if project["spec_revision"] != latest_spec or project["mock_revision"] != latest_mock:
        fail("최신 명세서·목업 revision과 프로젝트 포인터가 일치해야 합니다.", ErrorCode.STALE_VERSION)

    spec = conn.execute(
        "SELECT * FROM specs WHERE project_id=? AND revision=?",
        (studio_id, project["spec_revision"]),
    ).fetchone()
    mock = conn.execute(
        "SELECT * FROM mocks WHERE project_id=? AND revision=?",
        (studio_id, project["mock_revision"]),
    ).fetchone()
    if not spec or not spec["approved"] or not mock:
        fail("현재 명세서·목업 확정이 필요합니다.", ErrorCode.APPROVAL_REQUIRED)
    spec, mock = dict(spec), dict(mock)
    spec_payload, mock_payload = json.loads(spec["payload"]), json.loads(mock["payload"])
    if (
        digest(spec_payload) != spec["digest"]
        or digest(mock_payload) != mock["digest"]
        or mock["spec_revision"] != spec["revision"]
    ):
        fail("Studio 원본 버전·해시가 일치하지 않습니다.", ErrorCode.STALE_VERSION)
    screens = mock_payload.get("screens", [])
    screen_ids = {s["id"] for s in screens}
    if (
        not screen_ids
        or len(screen_ids) != len(screens)
        or screen_ids != {s["id"] for s in spec_payload.get("screens", [])}
    ):
        fail("현재 명세서와 목업의 화면 범위가 일치해야 합니다.")
    approvals = [
        dict(a)
        for a in conn.execute(
            "SELECT * FROM approvals WHERE project_id=? AND mock_revision=? ORDER BY screen_id",
            (studio_id, mock["revision"]),
        )
    ]
    if {a["screen_id"] for a in approvals} != screen_ids or any(
        a["digest"]
        != digest({"mock": mock["digest"], "spec": spec["digest"], "screen": a["screen_id"]})
        for a in approvals
    ):
        fail("모든 현재 화면 확정이 필요합니다.", ErrorCode.APPROVAL_REQUIRED)
    if conn.execute(
        "SELECT 1 FROM comments WHERE project_id=? AND mock_revision=? AND state='open'",
        (studio_id, mock["revision"]),
    ).fetchone():
        fail("고아 의견을 포함한 미해결 의견을 모두 처리해주세요.", ErrorCode.APPROVAL_REQUIRED)
    if conn.execute(
        "SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running')", (studio_id,)
    ).fetchone():
        fail("AI 생성 완료 후 인계해주세요.", ErrorCode.STALE_VERSION)
    requirements = spec_payload.get("requirements", [])
    if not requirements or len({r["id"] for r in requirements}) != len(requirements):
        fail("고유한 요구사항 ID가 필요합니다.")
    for req in requirements:
        if not isinstance(req.get("acceptance", []), list) or any(
            not isinstance(a, str) or not a.strip() for a in req.get("acceptance", [])
        ):
            fail("수용 기준은 비어 있지 않은 문자열 배열이어야 합니다.")
        req["criteria_ids"] = [
            f"{req['id']}:AC-{i + 1:02d}" for i in range(len(req.get("acceptance", [])))
        ]
    source = {
        "studio_id": studio_id,
        "project_version": project["version"],
        "brief": project["brief"],
        "spec_revision": spec["revision"],
        "mock_revision": mock["revision"],
        "spec_digest": spec["digest"],
        "mock_digest": mock["digest"],
        "spec_source": spec["source"],
        "approvals": approvals,
    }
    return {
        "project": project,
        "spec": spec,
        "mock": mock,
        "spec_payload": spec_payload,
        "mock_payload": mock_payload,
        "requirements": requirements,
        "screens": screens,
        "source": source,
        "bundle_digest": digest(source),
    }


def default_groups(bundle):
    """Suggested tasks: one per screen with the requirements it shows, plus one for the rest.

    One task per requirement ran every role once per requirement; each
    task runs the whole roster, so fewer, cohesive tasks cost far fewer AI runs.
    """
    order = [r["id"] for r in bundle["requirements"]]
    titles = {r["id"]: r["title"] for r in bundle["requirements"]}

    def title(ids):
        return titles[ids[0]] + (f" 외 {len(ids) - 1}건" if len(ids) > 1 else "")

    groups, taken = [], set()
    for screen in bundle["spec_payload"].get("screens", []):
        ids = [i for i in order if i in set(screen.get("requirement_ids", [])) and i not in taken]
        if ids:
            groups.append((title(ids), ids))
            taken.update(ids)
    rest = [i for i in order if i not in taken]
    if rest:
        groups.append((title(rest), rest))
    return groups


UI_MARKERS = {"react", "expo", "react-native", "flutter", "swift", "android"}
SERVER_MARKERS = {"spring-boot"}
# Reviews added from repository markers alone; the user may drop them for a task that
# does not touch them. Frontend/backend always follow the detected stack.
OPTIONAL_REVIEWS = {"infra", "db"}


def default_tasks(bundle, repositories):
    """(title, requirement ids, module) for the suggested tasks.

    Screen tasks go to the UI module; a project with a server module also gets one
    server task for every requirement, which the user can delete.
    """
    from agent_dev.adapters.skill_store import detect

    def markers(binding):
        path = binding.get("path", "")
        return set(detect(Path(path))) if path else set()

    kinds = {name: markers(binding) for name, binding in repositories.items()}
    first = next(iter(repositories))
    ui = next((n for n, m in kinds.items() if m & UI_MARKERS), None)
    server = next((n for n, m in kinds.items() if m & SERVER_MARKERS and n != ui), None)
    shown = {
        rid
        for screen in bundle["spec_payload"].get("screens", [])
        for rid in screen.get("requirement_ids", [])
    }
    groups = default_groups(bundle)
    if ui and server:
        # Requirements without a screen are covered by the server task.
        everything = [r["id"] for r in bundle["requirements"]]
        more = f" 외 {len(everything) - 1}건" if len(everything) > 1 else ""
        tasks = [(bundle["requirements"][0]["title"] + more, everything, server)]
        tasks += [(title, ids, ui) for title, ids in groups if set(ids) & shown]
    else:
        tasks = [
            (title, ids, (ui if set(ids) & shown else server) or first) for title, ids in groups
        ]
    if len({module for _, _, module in tasks}) > 1:
        tasks = [(f"[{module}] {title}", ids, module) for title, ids, module in tasks]
    return tasks


def workstreams(bundle, requirement_ids, path):
    """Roles a handoff task needs: frontend when it touches a screen or a UI stack,
    backend for a server stack, and the DB review for a database dependency."""
    from agent_dev.adapters.skill_store import detect

    shown = {
        rid
        for screen in bundle["spec_payload"].get("screens", [])
        for rid in screen.get("requirement_ids", [])
    }
    markers = set(detect(Path(path))) if path else set()
    streams = []
    # A server module showing a screen's requirements serves its API, not the screen.
    if markers & UI_MARKERS or (set(requirement_ids) & shown and not markers & SERVER_MARKERS):
        streams.append("frontend")
    if markers & SERVER_MARKERS:
        streams.append("backend")
    if markers & {"docker", "kubernetes", "terraform"}:
        streams.append("infra")
    return streams, ["db"] if "postgres" in markers else []


def _suggest_id(conn, table, base):
    candidate, suffix = base, 2
    while conn.execute("SELECT 1 FROM " + table + " WHERE id=?", (candidate,)).fetchone():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        fail("id: 영문·숫자·밑줄·하이픈 1~100자를 사용해주세요.")
    return value


def normalized_requirement(req, secrets=()):
    """Canonical import content, shared with requirement-change comparison."""
    safe = json.loads(canonical(redact(req, secrets)))
    safe["criteria_ids"] = [f"{safe['id']}:AC-{i + 1:02d}" for i in range(len(safe.get("acceptance", [])))]
    return safe


def _prepare(conn, bundle, params, *, required_requirement_ids=None, enforce_existing_intent=True):
    project_id = params.get("project_id")
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        fail("project_id: 등록된 프로젝트를 선택해주세요.")
    profile = conn.execute(
        "SELECT * FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if profile is None:
        fail("profile_version: 등록된 프로파일이 필요합니다.")
    for key, current in (
        ("profile_version", profile["version"]),
        ("expected_project_version", project["version"]),
        ("expected_source_version", bundle["project"]["version"]),
    ):
        if key in params and (type(params[key]) is not int or params[key] != current):
            fail(
                key + ": 버전이 변경되었습니다. 새 미리보기가 필요합니다.", ErrorCode.STALE_VERSION
            )
    config = json.loads(profile["content_json"])
    if digest(config) != profile["digest"]:
        fail("프로파일 해시가 일치하지 않습니다.")
    repositories = config.get("repositories", {})
    if not repositories:
        fail("등록된 저장소 모듈이 필요합니다.")
    gates = {g["name"]: g for g in config.get("verification", {}).get("commands", [])}
    base = (
        re.sub(r"[^A-Za-z0-9_-]", "-", str(project_id))[:40]
        + "-S"
        + str(bundle["spec"]["revision"])
    )
    existing = conn.execute(
        "SELECT * FROM handoff_imports WHERE project_id=? AND source_studio_id=? AND spec_digest=? AND mock_digest=?",
        (project_id, bundle["project"]["id"], bundle["spec"]["digest"], bundle["mock"]["digest"]),
    ).fetchone()
    design_id = _id(
        params.get("design_id")
        or (
            existing["design_id"]
            if existing
            else _suggest_id(conn, "design_documents", "D-" + base)
        )
    )
    plan_id = _id(
        params.get("plan_id")
        or (existing["plan_id"] if existing else _suggest_id(conn, "task_plans", "PLAN-" + base))
    )
    req_ids = {r["id"] for r in bundle["requirements"]}
    tasks = params.get("tasks")
    if tasks is None:
        tasks = [
            {
                "id": _suggest_id(conn, "tasks", f"T-{base}-{i + 1:02d}"),
                "title": title,
                "module_id": module,
                "requirement_ids": ids,
                "verification_gates": [
                    n for n, g in gates.items() if g.get("repository") == module
                ],
            }
            for i, (title, ids, module) in enumerate(default_tasks(bundle, repositories))
        ]
    validate_dag(tasks)
    if not tasks:
        fail("tasks: 모든 요구사항을 작업에 연결해주세요.")
    normalized = []
    for task in tasks:
        if set(task) - {
            "id",
            "title",
            "module_id",
            "requirement_ids",
            "verification_gates",
            "depends_on",
            "priority",
            "allowed_paths",
            "verification_mapping",
            # Echoed back from a preview; recomputed below, never trusted from input.
            "workstreams",
            "risk",
            "skip_reviews",
        }:
            fail("tasks: 지원하지 않는 필드입니다.")
        task_id = _id(task["id"])
        module = task.get("module_id")
        if module not in repositories:
            fail("module_id: 등록된 모듈을 선택해주세요.")
        if (
            not isinstance(task.get("requirement_ids"), list)
            or not task["requirement_ids"]
            or set(task["requirement_ids"]) - req_ids
        ):
            fail("requirement_ids: 현재 명세서의 요구사항을 선택해주세요.")
        selected_gates = task.get("verification_gates", [])
        if not isinstance(selected_gates, list) or any(
            g not in gates or gates[g].get("repository") != module for g in selected_gates
        ):
            fail("verification_gates: 선택 모듈의 등록된 검증만 사용할 수 있습니다.")
        allowed = repositories[module].get("allowed_paths", [])
        paths = task.get("allowed_paths", allowed)
        if (
            not isinstance(paths, list)
            or not paths
            or any(
                p not in allowed or Path(p).is_absolute() or ".." in Path(p).parts for p in paths
            )
        ):
            fail("allowed_paths: 등록된 모듈 범위 내 경로가 필요합니다.")
        row = conn.execute("SELECT plan_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and (not existing or row["plan_id"] != existing["plan_id"]):
            fail("task id: 이미 사용 중인 ID입니다.")
        verification_mapping = task.get("verification_mapping", [])
        if not isinstance(verification_mapping, list):
            fail("verification_mapping: 배열이 필요합니다.")
        for mapping in verification_mapping:
            if not isinstance(mapping, dict) or set(mapping) != {
                "requirement_id",
                "criteria_ids",
                "gate",
                "target",
            }:
                fail("verification_mapping: 요구사항·수용 기준·검증·대상이 필요합니다.")
            req = next(
                (r for r in bundle["requirements"] if r["id"] == mapping["requirement_id"]), None
            )
            gate = gates.get(mapping["gate"], {})
            target = mapping["target"]
            if (
                req is None
                or req["id"] not in task["requirement_ids"]
                or not isinstance(mapping["criteria_ids"], list)
                or not mapping["criteria_ids"]
                or set(mapping["criteria_ids"]) - set(req["criteria_ids"])
                or mapping["gate"] not in selected_gates
                or not isinstance(target, str)
                or target not in gate.get("command", [])
                or not ("::" in target and target.split("::")[0].endswith(".py"))
            ):
                fail(
                    "verification_mapping: 등록된 테스트 selector와 해당 요구사항의 수용 기준을 선택해주세요."
                )
        streams, risk = workstreams(
            bundle, task["requirement_ids"], repositories[module].get("path", "")
        )
        skip = task.get("skip_reviews", [])
        if not isinstance(skip, list) or set(skip) - OPTIONAL_REVIEWS:
            fail("skip_reviews: infra·db 검토만 뺄 수 있습니다.")
        # Only a review this task would get can be skipped; the rest is ignored.
        skipped = sorted(set(skip) & (set(streams) | set(risk)))
        streams = [s for s in streams if s not in skipped]
        risk = [r for r in risk if r not in skipped]
        normalized.append(
            {
                "id": task_id,
                "title": task["title"],
                "workstreams": streams,
                "risk": risk,
                "skip_reviews": skipped,
                "priority": task.get("priority", "medium"),
                "module_id": module,
                "requirement_ids": sorted(set(task["requirement_ids"])),
                "verification_gates": sorted(set(selected_gates)),
                "depends_on": task.get("depends_on", []),
                "allowed_paths": paths,
                "verification_mapping": sorted([dict(m, criteria_ids=sorted(set(m["criteria_ids"]))) for m in verification_mapping], key=canonical),
            }
        )
    required_ids = req_ids if required_requirement_ids is None else set(required_requirement_ids)
    if not required_ids <= set().union(*(set(t["requirement_ids"]) for t in normalized)):
        fail("requirement_ids: 백엔드 요구사항을 포함해 모든 요구사항에 작업 연결이 필요합니다.")
    for table, target in (("design_documents", design_id), ("task_plans", plan_id)):
        if conn.execute("SELECT 1 FROM " + table + " WHERE id=?", (target,)).fetchone() and (
            not existing
            or target != existing["design_id" if table == "design_documents" else "plan_id"]
        ):
            fail("id: 이미 사용 중인 설계·계획 ID입니다.")
    selection = {
        "project_id": project_id,
        "source_studio_id": bundle["project"]["id"],
        "profile_version": profile["version"],
        "expected_project_version": project["version"],
        "expected_source_version": bundle["project"]["version"],
        "design_id": design_id,
        "plan_id": plan_id,
        "tasks": normalized,
    }
    proof = {
        "source": bundle["source"],
        "profile_digest": profile["digest"],
        "selection": selection,
    }
    mappings = []
    for req in bundle["requirements"]:
        screen_ids = [
            s["id"]
            for s in bundle["spec_payload"]["screens"]
            if req["id"] in s.get("requirement_ids", [])
        ]
        mappings.append(
            {
                "requirement_id": req["id"],
                "title": req["title"],
                "criteria_ids": req["criteria_ids"],
                "linked_screens": screen_ids,
                "backend_only": not screen_ids,
            }
        )
    preview_digest = digest(proof)
    if existing and enforce_existing_intent:
        draft = conn.execute(
            "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='PlanDraftCreated'",
            (existing["plan_id"],),
        ).fetchone()
        if (
            draft is None
            or json.loads(draft["payload_json"]).get("preview_digest") != preview_digest
        ):
            fail(
                "이 명세·목업 버전은 이미 다른 작업 범위로 인계되었습니다. 범위를 바꾸려면 명세서를 고쳐 새 버전으로 "
                "저장·확정하고, 화면 검토에서 '이전 목업 이어서 쓰기'로 목업을 옮겨 화면을 확정한 뒤 다시 인계해주세요. "
                "AI 재생성은 필요 없습니다.",
                ErrorCode.STALE_VERSION,
            )
    return {
        "selection": selection,
        "preview_digest": preview_digest,
        "bundle_digest": bundle["bundle_digest"],
        "spec_digest": bundle["spec"]["digest"],
        "mock_digest": bundle["mock"]["digest"],
        "spec_revision": bundle["spec"]["revision"],
        "mock_revision": bundle["mock"]["revision"],
        "profile_digest": profile["digest"],
        "requirement_count": len(req_ids),
        "screen_count": len(bundle["screens"]),
        "mappings": mappings,
        "approval_required": ["design", "plan"],
        "runs_per_task": runs_per_task(config),
    }


def runs_per_task(profile):
    """Most AI invocations one task can take: one per session plus one per role outside a
    session (conditional roles may skip). Shown before handoff so cost is visible."""
    nodes = [
        n
        for n in (profile.get("workflow") or {}).get("document", {}).get("nodes", [])
        if n.get("kind") == "role"
    ]
    return len({n["session"] for n in nodes if n.get("session")}) + sum(
        1 for n in nodes if not n.get("session")
    )


def review_gate(conn, studio_id):
    """Open high-severity independent-review findings stop the handoff (spec_review GATES).

    Kept out of _read_bundle, which traceability also uses to compare the source.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='findings'").fetchone():
        return
    from agent_dev.domain.spec_review import blocking

    findings = [dict(r) for r in conn.execute("SELECT * FROM findings WHERE project_id=?", (studio_id,))]
    open_high = blocking(findings, "handoff")
    if open_high:
        fail(
            f"독립 검토의 심각한 지적 {len(open_high)}건이 열려 있습니다. Studio에서 반영하거나 이유를 적어 "
            "수용·반박한 뒤 개발 인계해주세요.",
            ErrorCode.APPROVAL_REQUIRED,
        )


def preview(canonical_db, studio_root, params):
    with contextlib.closing(_open(_studio_db_path(studio_root))) as studio:
        studio.execute("BEGIN")
        bundle = _read_bundle(studio, params.get("source_studio_id"))
        review_gate(studio, bundle["project"]["id"])
    with contextlib.closing(canonical_db.connect()) as conn:
        conn.execute("BEGIN")
        return _prepare(conn, bundle, params)


def commit(canonical_db, studio_root, params):
    request_id = _id(params.get("request_id"))
    if not params.get("preview_digest"):
        fail("preview_digest: 먼저 미리보기를 확인해주세요.")
    key = "handoff:" + request_id
    payload_digest = digest(params)
    studio = None
    try:
        with canonical_db.transaction() as conn:
            command = conn.execute("SELECT * FROM commands WHERE key=?", (key,)).fetchone()
            if command:
                if command["request_digest"] != payload_digest:
                    fail("request_id: 다른 입력에 사용된 키입니다.")
                return json.loads(command["result_json"])
            # Canonical ownership precedes Studio ownership. Keep Studio open through COMMIT.
            studio = _open(_studio_db_path(studio_root), writable=True)
            studio.execute("BEGIN IMMEDIATE")
            bundle = _read_bundle(studio, params.get("source_studio_id"))
            review_gate(studio, bundle["project"]["id"])
            prepared = _prepare(conn, bundle, params)
            if prepared["preview_digest"] != params["preview_digest"]:
                fail("미리보기 이후 원본·대상·매핑이 변경되었습니다.", ErrorCode.STALE_VERSION)
            existing = conn.execute(
                "SELECT * FROM handoff_imports WHERE project_id=? AND source_studio_id=? AND spec_digest=? AND mock_digest=?",
                (
                    params["project_id"],
                    params["source_studio_id"],
                    bundle["spec"]["digest"],
                    bundle["mock"]["digest"],
                ),
            ).fetchone()
            if existing:
                result = {
                    "import_id": existing["id"],
                    "design_id": existing["design_id"],
                    "plan_id": existing["plan_id"],
                }
            else:
                result = _write_handoff(conn, canonical_db, bundle, prepared, key)
            conn.execute(
                "INSERT INTO commands VALUES(?,?, 'done',?,?)",
                (key, payload_digest, canonical(result), time.time()),
            )
        return result
    finally:
        if studio is not None:
            studio.rollback()
            studio.close()


def _write_handoff(conn, db, bundle, prepared, key):
    from agent_dev.adapters.secrets import registered_secrets

    repo = WorkflowRepository(db, secrets=tuple(registered_secrets(db.path.parent)))
    selection = prepared["selection"]
    project_id, design_id, plan_id = (
        selection["project_id"],
        selection["design_id"],
        selection["plan_id"],
    )
    import_id = identifier()
    content = (
        "## Scope\n"
        + bundle["project"]["title"]
        + "\n### Original brief\n"
        + bundle["project"]["brief"]
        + "\n## Acceptance\n"
        + canonical(bundle["spec_payload"])
        + "\n## Constraints\nExplicit design and plan approval required. Registered module paths and verification gates only.\n### Studio mock snapshot\n"
        + canonical(bundle["mock_payload"])
    )
    content = redact(content, repo.secrets)
    conn.execute(
        "INSERT INTO design_documents VALUES(?,?,?,?,?,'user_review')",
        (design_id, project_id, 1, digest(content), content),
    )
    pending = []
    for task in selection["tasks"]:
        pending.append(
            dict(
                task,
                handoff_scope={
                    "profile_version": selection["profile_version"],
                    "profile_digest": prepared["profile_digest"],
                    "module_id": task["module_id"],
                    "allowed_paths": task["allowed_paths"],
                    "verification_gates": task["verification_gates"],
                },
            )
        )
    pending = json.loads(repo._json(pending))
    conn.execute(
        "INSERT INTO task_plans VALUES(?,?,?,?,?,?, 'approval_pending')",
        (plan_id, project_id, design_id, 1, 1, digest(pending)),
    )
    repo._event(conn, project_id, "DesignRevised", {"id": design_id, "version": 1})
    repo._event(
        conn,
        plan_id,
        "PlanDraftCreated",
        {
            "pending_tasks": pending,
            "profile_version": selection["profile_version"],
            "preview_digest": prepared["preview_digest"],
        },
    )
    conn.execute(
        "INSERT INTO handoff_imports VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            import_id,
            project_id,
            bundle["project"]["id"],
            bundle["spec"]["digest"],
            bundle["mock"]["digest"],
            bundle["bundle_digest"],
            design_id,
            1,
            plan_id,
            key,
            time.time(),
        ),
    )
    for req in bundle["requirements"]:
        safe = normalized_requirement(req, repo.secrets)
        conn.execute(
            "INSERT INTO imported_requirements VALUES(?,?,?,?)",
            (import_id, req["id"], canonical(safe), digest(safe)),
        )
    for mapping in prepared["mappings"]:
        for screen in mapping["linked_screens"]:
            conn.execute(
                "INSERT INTO requirement_links(id,import_id,requirement_id,kind,target_ref,evidence_digest,state) VALUES(?,?,?,'screen',?,?,'mapped')",
                (
                    identifier(),
                    import_id,
                    mapping["requirement_id"],
                    screen,
                    bundle["mock"]["digest"],
                ),
            )
    for task in pending:
        for req_id in task["requirement_ids"]:
            conn.execute(
                "INSERT INTO requirement_links(id,import_id,requirement_id,kind,target_ref,module_id,evidence_digest,state) VALUES(?,?,?,'plan_task',?,?,?,'mapped')",
                (
                    identifier(),
                    import_id,
                    req_id,
                    task["id"],
                    task["module_id"],
                    prepared["preview_digest"],
                ),
            )
    repo._event(
        conn,
        project_id,
        "StudioHandoffImported",
        {
            "import_id": import_id,
            "plan_id": plan_id,
            "preview_digest": prepared["preview_digest"],
            "source": bundle["source"],
        },
    )
    return {"import_id": import_id, "design_id": design_id, "plan_id": plan_id}
