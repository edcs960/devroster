"""Read-only home facts and explicit scoped handoff/traceability commands."""

import json
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from agent_dev.adapters import studio_handoff, traceability
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.local_web import LocalWeb


class JourneyWeb(LocalWeb):
    prefix = page = "home"
    page_assets: ClassVar = {
        "/traceability": ("traceability.html", "text/html"),
        "/traceability/": ("traceability.html", "text/html"),
        "/traceability.js": ("traceability.js", "text/javascript"),
    }
    get_routes: ClassVar = {"/api/home/bootstrap", "/api/traceability/query", "/api/traceability/detail"}
    post_routes: ClassVar = {"/api/handoff/preview", "/api/handoff/commit", "/api/traceability/link"}

    def __init__(self, root):
        from agent_dev.adapters.workflow_runtime import project_root
        from agent_dev.adapters.workspace import configuration_root

        super().__init__(root)
        self._config = configuration_root(Path(root))
        self._root = project_root(self._config)

    @classmethod
    def matches(cls, path):
        return path in {"/home", "/home/", "/home.js", "/journey.css", *cls.page_assets} or any(
            path.startswith(p) for p in ("/api/home/", "/api/handoff/", "/api/traceability/")
        )

    def _db(self, read_only=True):
        from agent_dev.adapters.workspace_locator import state_db_path

        path = state_db_path(self._config)
        if not path.is_file():
            raise DomainError(
                ErrorCode.INVALID_INPUT, "프로젝트 설정에서 작업 공간을 먼저 준비해주세요."
            )
        return Database(path, read_only=read_only)

    def handle(self, handler):
        path = urlsplit(handler.path).path
        allowed = self.get_routes if handler.command == "GET" else self.post_routes
        if path.startswith("/api/") and path not in allowed:
            from agent_dev.ui.responses import send

            send(handler, {"error": "요청 경로를 확인해주세요."}, status=400)
            return
        super().handle(handler)

    def get(self, action, query):
        if action == "bootstrap":
            return self._home_bootstrap(query)
        if action == "query":
            return traceability.query(self._db(), query)
        if action == "detail":
            return traceability.detail(self._db(), query["link_id"])
        raise DomainError(ErrorCode.INVALID_INPUT, "조회 기능을 찾을 수 없습니다.")

    def post(self, action, payload):
        if action == "preview":
            return studio_handoff.preview(self._db(), self._root, payload)
        if action == "commit":
            result = studio_handoff.commit(self._db(False), self._root, payload)
            from agent_dev.adapters.ai_hooks import studio_event

            studio_event(self._root, {"what": "handoff", "studio_project": payload.get("source_studio_id", ""), "plan": payload.get("plan_id", "")})
            return result
        if action == "link":
            return traceability.create_link(self._db(False), payload)
        raise DomainError(ErrorCode.INVALID_INPUT, "실행 기능을 찾을 수 없습니다.")

    def _execution_enabled(self):
        from agent_dev.adapters.workspace_locator import settings_root

        # Resolve again: a project first saved after the app opened has a new settings folder.
        path = settings_root(self._root) / "config/runtime.json"
        try:
            flags = json.loads(path.read_text(encoding="utf-8")).get("orchestrator_v2", {})
        except (OSError, ValueError):
            return False
        return flags.get("execute") is True

    def _home_bootstrap(self, query):
        empty = {
            "projects": [],
            "selected": None,
            "readiness": [],
            "tasks_in_progress": [],
            "current_task": None,
            "primary_action": {"href": "/setup", "label": "프로젝트 준비하기"},
            "profile": None,
            "imports": [],
            "drafts": [],
        }
        from agent_dev.adapters.workspace_locator import state_db_path

        if not state_db_path(self._config).is_file():
            return empty
        db = self._db()
        projects = db.rows("SELECT id,name,state,version FROM projects ORDER BY created_at DESC")
        project_id = query.get("project_id") or (projects[0]["id"] if projects else "")
        if not project_id:
            return dict(empty, projects=projects)
        selected = next((p for p in projects if p["id"] == project_id), None)
        if not selected:
            raise DomainError(ErrorCode.INVALID_INPUT, "등록된 프로젝트를 선택해주세요.")
        rows = db.rows(
            "SELECT * FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (project_id,),
        )
        row = rows[0] if rows else None
        profile = json.loads(row["content_json"]) if row else {}
        repositories = profile.get("repositories", {})
        folders = bool(repositories) and all(
            (self._root / b["path"]).is_dir() for b in repositories.values()
        )
        runners = [r for r in profile.get("runners", []) if r.get("enabled")]
        checked = [r for r in runners if r.get("checked_at")]
        ai_ready = bool(checked) and any(r.get("authenticated") for r in checked)
        connections = [
            json.loads(r["content_json"])
            for r in db.rows(
                "SELECT content_json FROM tool_connections WHERE project_id=?", (project_id,)
            )
        ]
        checked_mcp = [
            c
            for c in connections
            if c.get("integration") and c.get("enabled") and c.get("checked_at")
        ]
        mcp_ready = bool(checked_mcp) and all(c.get("state") == "ready" for c in checked_mcp)
        runs = db.rows(
            "SELECT vr.* FROM verification_runs vr JOIN tasks t ON t.id=vr.task_id WHERE t.project_id=? ORDER BY vr.created_at DESC LIMIT 1",
            (project_id,),
        )

        def readiness(key, label, status, checked_at, reason, action):
            return {
                "key": key,
                "item": label,
                "status": status,
                "checked": status == "ready",
                "checked_at": checked_at,
                "reason": reason,
                "action": action,
            }

        readiness_rows = [
            readiness(
                "folders",
                "저장소 폴더",
                "ready" if folders else "missing",
                time.time(),
                "등록된 저장소 폴더 확인" if folders else "등록 폴더가 없거나 접근할 수 없습니다.",
                "/setup",
            ),
            readiness(
                "ai",
                "AI 연결",
                "ready" if ai_ready else "failed" if checked else "unknown",
                max((r["checked_at"] for r in checked), default=None),
                "마지막 명시적 인증 확인 결과"
                if checked
                else "연결 검사 기록이 없습니다. AI 도구 메뉴에서 로그인·연결 상태를 확인해주세요.",
                "/ai",
            ),
            readiness(
                "verification",
                "검증",
                "ready"
                if runs and runs[0]["state"] == "passed"
                else "failed"
                if runs
                else "unknown",
                runs[0]["created_at"] if runs else None,
                "마지막 실제 실행 결과입니다. 현재 소스 검증 여부는 요구사항 근거에서 확인하세요."
                if runs
                else "등록된 검증 "
                + str(len(profile.get("verification", {}).get("commands", [])))
                + "개 · 실제 실행 기록 없음",
                "/agent?operation=task.verify",
            ),
            readiness(
                "mcp",
                "MCP 연결",
                "ready" if mcp_ready else "failed" if checked_mcp else "unknown",
                max((c["checked_at"] for c in checked_mcp), default=None),
                "마지막 명시적 연결 검사 결과"
                if checked_mcp
                else "연결 검사를 실행하지 않았습니다. 필요하면 연결 화면에서 검사하세요.",
                "/integrations",
            ),
        ]
        tasks = db.rows(
            "SELECT id,title,state,version FROM tasks WHERE project_id=? AND state NOT IN ('completed','cancelled') ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'verifying' THEN 1 WHEN 'user_review' THEN 2 ELSE 3 END,rowid DESC LIMIT 20",
            (project_id,),
        )
        current = tasks[0] if tasks else None
        tables = {r["name"] for r in db.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        imports = (
            db.rows(
                "SELECT id,source_studio_id,design_id,design_version,plan_id,created_at FROM handoff_imports WHERE project_id=? ORDER BY created_at DESC",
                (project_id,),
            )
            if "handoff_imports" in tables
            else []
        )
        drafts = db.rows(
            "SELECT p.id,p.design_id,p.design_version,p.version,p.state,d.state AS design_state FROM task_plans p JOIN design_documents d ON d.id=p.design_id AND d.version=p.design_version WHERE p.project_id=? AND p.state!='approved' ORDER BY p.rowid DESC",
            (project_id,),
        )
        if not folders:
            primary = {"href": "/setup", "label": "저장소 폴더 준비하기"}
        elif drafts:
            draft = drafts[0]
            primary = (
                {
                    "href": "/agent?operation=design.approve&design_id="
                    + draft["design_id"]
                    + "&version="
                    + str(draft["design_version"]),
                    "label": "설계 명시적으로 승인하기",
                }
                if draft["design_state"] != "approved"
                else {
                    "href": "/agent?operation=plan.approve&plan_id=" + draft["id"],
                    "label": "계획 명시적으로 승인하기",
                }
            )
        elif current and current["state"] == "blocked" and (
            gate := db.rows(
                "SELECT node_id FROM workflow_node_runs WHERE task_id=? AND state='awaiting_approval' ORDER BY generation DESC LIMIT 1",
                (current["id"],),
            )
        ):
            # A workflow gate waits for the user; that is a normal step, not a failure.
            primary = {
                "href": "/workflows?task=" + current["id"],
                "label": "단계 승인하기 · " + gate[0]["node_id"],
            }
        elif current and current["state"] in {"pr_ready", "user_review"}:
            # After verification: publish (commit locally or open PRs), review, then complete.
            published = db.rows(
                "SELECT id,repository FROM publish_records WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                (current["id"],),
            )
            definition = db.rows("SELECT definition_json FROM tasks WHERE id=?", (current["id"],))
            module = (
                json.loads(definition[0]["definition_json"]).get("handoff_scope", {}).get("module_id")
                if definition
                else None
            )
            if not published:
                primary = {
                    "href": "/agent?operation=task.publish&task_id="
                    + current["id"]
                    + ("&repository=" + module if module else ""),
                    "label": "변경 게시하기 · 검증한 변경을 작업 브랜치에 커밋",
                }
            elif current["state"] == "pr_ready":
                primary = {
                    "href": "/agent?operation=task.review&task_id=" + current["id"],
                    "label": "게시 결과 확인하기",
                }
            else:
                primary = {
                    "href": "/agent?operation=task.complete&task_id="
                    + current["id"]
                    + "&publish_id="
                    + published[0]["id"],
                    "label": "작업 완료하기 · 기본 브랜치에 반영",
                }
        elif current and current["state"] == "ready" and not self._execution_enabled():
            primary = {
                "href": "/agent?operation=task.explain&task_id=" + current["id"],
                "label": "실행이 꺼져 있어 시작할 수 없습니다 · 켜는 방법 확인하기",
            }
        elif current:
            operation, label = {
                "verifying": ("task.verify", "현재 작업 검증하기"),
                "pr_ready": ("task.review", "개발 근거·검토 확인하기"),
                "user_review": ("task.review", "사용자 검토 근거 확인하기"),
                "blocked": ("task.explain", "막힌 이유와 재개 조건 확인하기"),
                "waiting_for_capacity": ("task.explain", "실행 도구 대기 상태 확인하기"),
                "running": ("task.explain", "실행 중인 작업 확인하기"),
                "approval_pending": ("task.explain", "작업 승인 조건 확인하기"),
                "feedback_required": ("task.explain", "피드백·새 계획 확인하기"),
            }.get(current["state"], ("task.run", "현재 작업 실행 준비하기"))
            primary = {
                "href": "/agent?operation=" + operation + "&task_id=" + current["id"],
                "label": label,
            }
        else:
            primary = {"href": "/studio", "label": "기획·화면 확정하고 개발 계획 준비하기"}
        public_profile = (
            {
                "version": row["version"],
                "digest": row["digest"],
                "modules": [
                    {
                        "id": name,
                        "allowed_paths": binding.get("allowed_paths", []),
                        "gates": [
                            {
                                "name": g["name"],
                                "targets": [
                                    a
                                    for a in g.get("command", [])
                                    if isinstance(a, str)
                                    and "::" in a
                                    and a.split("::")[0].endswith(".py")
                                ],
                            }
                            for g in profile.get("verification", {}).get("commands", [])
                            if g.get("repository") == name
                        ],
                    }
                    for name, binding in repositories.items()
                ],
            }
            if row
            else None
        )
        return redact(
            dict(
                empty,
                projects=projects,
                selected=selected,
                readiness=readiness_rows,
                tasks_in_progress=tasks,
                current_task=current,
                primary_action=primary,
                profile=public_profile,
                imports=imports,
                drafts=drafts,
            )
        )
