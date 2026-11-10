"""Quick fix: a short description, one confirmation, the usual approvals and snapshot.

The design document, the one-task plan, both approvals and the snapshot are written exactly
as `design refine/approve` and `plan create/approve` would; the confirmation the user gives
in the operator console is recorded as those approvals (`fast_track` in the event). The task
is limited to one module and the quick-fix workflow (developer → qa → reviewer), with the
module's write scope and verification gates frozen like a Studio handoff.
"""

import secrets
import time

from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError, ErrorCode

TITLE_LIMIT = 120
DESCRIPTION_LIMIT = 4000
# Same names as the workflow editor (workflows.js).
ROLE_NAMES = {"developer": "개발", "qa": "QA", "reviewer": "코드 검토"}
DEFAULT_ACCEPTANCE = (
    "- 설명한 문제가 더 이상 재현되지 않는다.\n"
    "- 프로젝트의 필수 검증 명령이 모두 통과한다."
)


def invalid(message):
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def _project(store, project_id):
    projects = [row["id"] for row in store.db.rows("SELECT id FROM projects ORDER BY id")]
    if project_id:
        if project_id not in projects:
            invalid("등록된 프로젝트를 선택해주세요.")
        return project_id
    if len(projects) != 1:
        invalid("프로젝트가 여러 개입니다. 프로젝트를 지정해주세요.")
    return projects[0]


def _profile(store, project_id):
    import json

    rows = store.db.rows(
        "SELECT version,digest,content_json FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
        (project_id,),
    )
    if not rows:
        invalid("프로젝트 설정을 먼저 저장해주세요.")
    return rows[0], json.loads(rows[0]["content_json"])


def preview(store, module, title, description, project_id=""):
    """What would be created: no storage is written."""
    from agent_dev.adapters.workflow_definitions import preset

    project_id = _project(store, project_id)
    title, description = (title or "").strip(), (description or "").strip()
    if not title or len(title) > TITLE_LIMIT or "\n" in title:
        invalid(f"제목은 한 줄, {TITLE_LIMIT}자 이내로 입력해주세요.")
    if not description or len(description) > DESCRIPTION_LIMIT:
        invalid(f"고칠 내용을 {DESCRIPTION_LIMIT}자 이내로 입력해주세요.")
    row, profile = _profile(store, project_id)
    repositories = profile.get("repositories", {})
    if module not in repositories:
        invalid("등록된 모듈을 선택해주세요: " + ", ".join(sorted(repositories)))
    allowed = list(repositories[module].get("allowed_paths", []))
    if not allowed:
        invalid("이 모듈에는 수정 허용 경로가 없습니다. 프로젝트 설정에서 지정해주세요.")
    verification = profile.get("verification", {})
    gates = [c["name"] for c in verification.get("commands", []) if c.get("repository") == module]
    design = (
        f"# 빠른 수정: {title}\n\n"
        f"## 범위\n{description}\n\n"
        f"- 모듈: {module}\n- 수정 허용 경로: {', '.join(allowed)}\n\n"
        f"## 수용 기준\n{DEFAULT_ACCEPTANCE}\n\n"
        "## 제약\n"
        "- 위 모듈의 수정 허용 경로 안에서만 바꾼다.\n"
        "- 빠른 수정 흐름(개발 → QA → 코드 검토)으로 진행한다. 설계 변경이나 다른 모듈이 필요하면 멈추고 알린다.\n"
    )
    graph = preset("quick-fix")
    scope = {
        "module_id": module,
        "allowed_paths": allowed,
        "verification_gates": gates,
        "profile_version": row["version"],
        "profile_digest": row["digest"],
    }
    return {
        "project_id": project_id,
        "module": module,
        "title": title,
        "design": design,
        "workflow": "quick-fix",
        "roles": [n["role_id"] for n in graph["nodes"] if n.get("kind") == "role"],
        "role_names": [ROLE_NAMES.get(n["role_id"], n["role_id"]) for n in graph["nodes"] if n.get("kind") == "role"],
        "allowed_paths": allowed,
        "verification_gates": gates,
        "scope": scope,
        "graph": graph,
        "preview_digest": digest({"design": design, "scope": scope, "graph": graph}),
    }


def create(runtime, module, title, description, actor, project_id=""):
    """Design + approval, one-task plan + approval, snapshot. Returns the new task id."""
    store = runtime.store
    plan = preview(store, module, title, description, project_id)
    stamp = time.strftime("%Y%m%d", time.gmtime())
    suffix = secrets.token_hex(3).upper()
    design_id, plan_id, task_id = (f"QF-{stamp}-{suffix}{part}" for part in ("-D", "-P", ""))
    version = store.design(plan["project_id"], design_id, plan["design"])
    store.approve("design", design_id, version, actor)
    store.create_plan(
        plan["project_id"],
        plan_id,
        design_id,
        version,
        [
            {
                "id": task_id,
                "title": plan["title"],
                "fast_track": True,
                "handoff_scope": plan["scope"],
                "config_override": {"workflow": {"document": plan["graph"]}},
            }
        ],
    )
    store.approve("plan", plan_id, 1, actor)
    snapshot = store.create_snapshot(task_id, runtime.resolve_snapshot(task_id))
    store.event(task_id, "QuickFixCreated", {"design_id": design_id, "plan_id": plan_id, "actor": actor})
    return {
        "task_id": task_id,
        "design_id": design_id,
        "plan_id": plan_id,
        "snapshot": snapshot,
        "preview_digest": plan["preview_digest"],
    }
