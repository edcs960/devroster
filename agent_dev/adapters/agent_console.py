"""Structured local operator actions, backed by the canonical application services.

Only POST submit creates storage. HTTP reads never instantiate WorkflowRuntime.
Web jobs reuse command records and fenced leases, without changing the schema.
"""

import argparse
import json
import multiprocessing
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn

from agent_dev.adapters.command_line import dispatch, parser
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.adapters.workflow_runtime import project_root
from agent_dev.adapters.workspace import configuration_root
from agent_dev.domain.content import canonical, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode

GROUPS = {
    "task": "작업 실행",
    "project": "프로젝트",
    "design": "설계 승인",
    "plan": "작업 계획",
    "feedback": "테스트 피드백",
    "knowledge": "프로젝트 지식",
    "tool": "도구 권한",
    "refactor": "전환·운영",
    "backup": "백업·복구",
    "preset": "프로젝트 템플릿",
    "view": "화면 설정",
    "doctor": "환경 진단",
    "upgrade": "업그레이드",
    "notification": "알림",
}
LABELS = {
    "task_id": "작업 ID",
    "project_id": "프로젝트 ID",
    "design_id": "설계 ID",
    "plan_id": "계획 ID",
    "version": "설계 버전",
    "publish_id": "게시 결과 ID",
    "repository": "저장소 모듈 이름",
    "source": "문서 내용",
    "profile": "프로젝트 프로필 JSON",
    "feedback_id": "피드백 ID",
    "candidate_id": "지식 후보 ID",
    "evidence_id": "검증 증거 ID",
    "query": "검색어",
    "commit": "근거 커밋",
    "connection_id": "연결 ID",
    "capability": "허용 기능",
    "scope": "허용 범위",
    "phase_id": "단계 ID",
    "destination": "결과 파일 이름",
    "preset": "템플릿",
    "mode": "전환 모드",
    "release_id": "릴리스 태그",
}
ACTION_LABELS = {
    "run": "실행",
    "resume": "이어서 실행",
    "verify": "검증",
    "cancel": "작업 취소",
    "checkpoint": "체크포인트 저장",
    "publish": "게시",
    "review": "검토",
    "complete": "완료",
    "list": "목록",
    "show": "상세",
    "explain": "상태·차단 원인",
    "usage": "사용량",
    "register": "등록",
    "profile": "프로필 개정",
    "refine": "설계 문서 등록",
    "approve": "승인",
    "create": "생성",
    "add": "추가",
    "plan": "계획 전환",
    "candidate": "후보 등록",
    "confirm": "검증 확인",
    "search": "검색",
    "deprecate": "사용 중지",
    "grant": "권한 승인",
    "migrate": "데이터 이전",
    "compare": "비교",
    "export": "내보내기",
    "restore": "별도 폴더에 복원",
    "rollback-drill": "복구 연습",
    "rollback": "이전 실행기로 전환",
    "observe-release": "릴리스 기록",
    "status": "상태",
    "retention": "보관 정리",
    "configure": "전환 모드 설정",
    "inspect": "검사",
    "write": "템플릿 저장",
    "snapshot": "화면 상태",
    "settings": "설정 조회",
    "set-settings": "화면 설정 저장",
    "drain": "대기 알림 조회",
}
LINKS = {
    "workflow": "/workflows",
    "integration": "/integrations",
    "figma": "/references",
    "stitch": "/references",
    "source": "/source",
    "studio": "/studio",
    "setup": "/setup",
    "dashboard": "/dashboard",
    "init": "/setup",
}
# Explicit operation allowlist: adding a CLI action does not silently grant it to HTTP.
ALLOWED = {
    "task": {
        "list",
        "show",
        "explain",
        "usage",
        "run",
        "resume",
        "verify",
        "publish",
        "review",
        "complete",
        "cancel",
        "checkpoint",
    },
    "project": {"register", "profile", "show"},
    "design": {"refine", "approve"},
    "plan": {"create", "approve"},
    "feedback": {"add", "plan"},
    "knowledge": {"candidate", "confirm", "search", "deprecate"},
    "tool": {"register", "grant"},
    "refactor": {
        "migrate",
        "compare",
        "export",
        "restore",
        "rollback-drill",
        "rollback",
        "observe-release",
        "status",
        "retention",
        "configure",
    },
    "backup": {"create", "inspect", "restore"},
    "preset": {"list", "show", "write"},
    "view": {"snapshot", "settings", "set-settings"},
    "doctor": {""},
    "upgrade": {""},
    "notification": {"drain"},
}
READS = {
    "list",
    "show",
    "explain",
    "usage",
    "search",
    "compare",
    "status",
    "inspect",
    "snapshot",
    "settings",
}
OPTIONS = {
    "task.list": {"limit", "page", "search", "state", "project"},
    "task.explain": {"limit"},
    "task.publish": {"dry_run"},
    "refactor.migrate": {"dry_run"},
    "refactor.retention": {"apply"},
    "upgrade": {"apply", "recover"},
    "doctor": {"check_auth"},
}


def invalid(message) -> NoReturn:
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def commands():
    spaces = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
    result = {}
    for namespace, space in spaces.choices.items():
        child = next((a for a in space._actions if isinstance(a, argparse._SubParsersAction)), None)
        for action, command in child.choices.items() if child else [("", space)]:
            key = namespace + ("." + action if action else "")
            if action not in ALLOWED.get(namespace, set()):
                continue
            fields = []
            for field in command._actions:
                if not field.option_strings and field.dest not in {"namespace", "action"}:
                    kind = "text"
                    if field.dest in {"source", "profile"}:
                        kind = (
                            "project-file"
                            if namespace == "knowledge"
                            else "artifact"
                            if namespace in {"backup", "refactor"}
                            and action in {"inspect", "restore"}
                            else "document"
                        )
                    fields.append(
                        {
                            "name": field.dest,
                            "label": LABELS.get(field.dest, field.dest),
                            "type": kind,
                            "required": True,
                        }
                    )
            result[key] = {
                "id": key,
                "namespace": namespace,
                "action": action,
                "group": GROUPS[namespace],
                "label": (
                    "복원 검사 (새 폴더)"
                    if key == "backup.restore"
                    else ACTION_LABELS.get(action, GROUPS[namespace])
                ),
                "fields": fields,
                "options": sorted(OPTIONS.get(key, set())),
                "mutates": action not in READS,
                "background": True,
            }
    return result


def catalog():
    items = list(commands().values())
    spaces = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))
    for namespace, route in LINKS.items():
        space = spaces.choices.get(namespace)
        child = (
            next((a for a in space._actions if isinstance(a, argparse._SubParsersAction)), None)
            if space
            else None
        )
        items.append(
            {
                "id": namespace,
                "group": "전용 화면",
                "label": namespace,
                "route": route,
                "actions": list(child.choices) if child else [namespace],
                "state": "specialized",
            }
        )
    return {
        "operations": items,
        "links": [{"label": label, "route": route} for label, route in LINKS.items()],
    }


class AgentConsole:
    def __init__(self, root, dispatcher=None):
        self.root = configuration_root(Path(root).resolve())
        from agent_dev.adapters.storage_registry import workspace_locations

        self.workspace = project_root(self.root)
        self.locations = workspace_locations(self.root)
        self.db_path = self.locations.state_db
        if self.db_path.is_symlink() or not any(
            self.db_path.resolve().is_relative_to(scope)
            for scope in (self.workspace, self.locations.config_root)
        ):
            invalid("작업 저장소가 현재 프로젝트 범위를 벗어났습니다.")
        self.uploads = self.locations.runtime_root / "web-inputs"
        self.artifacts = self.locations.runtime_root / "web-artifacts"
        self.dispatcher = dispatcher
        self.lock = threading.Lock()
        self.processes = {}
        self.capture_progress = {}
        self.owner = uuid.uuid4().hex
        from agent_dev.adapters.secrets import registered_secrets

        self.secrets = registered_secrets(self.root)

    def _private(self, folder):
        if folder.is_symlink() or not folder.resolve().is_relative_to(self.workspace):
            invalid("프로젝트 저장 폴더 범위를 확인해주세요.")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder.chmod(0o700)

    def _artifact(self, value, *, existing=False):
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value
        ):
            invalid("파일 이름만 입력해주세요. 경로는 서버가 지정합니다.")
        path = self.artifacts / value
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(self.artifacts.resolve())
            or self.artifacts.is_symlink()
        ):
            invalid("파일 경로가 허용 범위를 벗어났습니다.")
        if existing and not path.is_file():
            invalid("이 화면에서 만든 백업·내보내기 파일을 선택해주세요.")
        return path

    def _validate_profile(self, profile):
        from agent_dev.adapters.project_setup import ProjectSetup

        setup = ProjectSetup(self.root)
        _, _, current = setup.current_profile()
        baseline = current or setup.initial_profile()
        # Runner binaries, credentials, tool grants and verification programs are
        # configured by the dedicated validated setup screen, never by raw JSON.
        for key in ("runners", "verification", "tools", "environment_keys", "notification"):
            if key in profile and profile[key] != baseline.get(key):
                invalid("실행기·검증·연결 설정은 프로젝트 설정 화면에서 변경해주세요: " + key)
        for binding in profile.get("repositories", {}).values():
            path = (self.workspace / binding["path"]).resolve()
            if not path.is_relative_to(self.workspace):
                invalid("저장소는 현재 프로젝트 범위에 있어야 합니다.")

        def paths(value):
            for key, item in value.items():
                if key in {"role_file", "path"} and isinstance(item, str):
                    if not (self.workspace / item).resolve().is_relative_to(self.workspace):
                        invalid("프로필 파일 경로가 프로젝트를 벗어났습니다.")
                elif key in {"allowed_paths", "test_paths", "document_paths"}:
                    if any(Path(p).is_absolute() or ".." in Path(p).parts for p in item):
                        invalid("프로필 범위는 프로젝트 내부 상대 경로여야 합니다.")
                elif isinstance(item, dict):
                    paths(item)
                elif isinstance(item, list):
                    for child in item:
                        if isinstance(child, dict):
                            paths(child)

        paths(profile)

    def prepare(self, payload, *, materialize=False, request_id=""):
        if set(payload) - {"operation", "fields", "options", "request_id", "preview_digest"}:
            invalid("허용되지 않은 요청 항목입니다.")
        op = commands().get(payload.get("operation"))
        if not op:
            invalid("지원하는 기능을 선택해주세요.")
        values, options = payload.get("fields", {}), payload.get("options", {})
        if (
            not isinstance(values, dict)
            or not isinstance(options, dict)
            or set(values) != {f["name"] for f in op["fields"]}
            or set(options) - set(op["options"])
        ):
            invalid("필수 입력과 옵션을 확인해주세요.")
        positional = []
        prepared = {}
        for field in op["fields"]:
            key, value = field["name"], values[field["name"]]
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.encode()) > (500_000 if field["type"] == "document" else 1000)
                or "\x00" in value
            ):
                invalid("입력 길이와 형식을 확인해주세요: " + field["label"])
            if field["type"] == "document":
                markdown = op["id"] in {"design.refine", "feedback.add", "knowledge.candidate"}
                document: Any = value if markdown else json.loads(value)
                if not markdown and not isinstance(
                    document, list if op["id"] == "plan.create" else dict
                ):
                    invalid("작업 계획은 JSON 배열, 나머지 입력은 JSON 객체가 필요합니다.")
                if op["id"] == "plan.create":
                    for item in document:
                        if not isinstance(item, dict):
                            invalid("작업 계획 항목은 JSON 객체여야 합니다.")
                        if item.get("config_override"):
                            self._validate_profile(item["config_override"])
                if op["id"] == "view.set-settings":
                    assert isinstance(document, dict)
                    from agent_dev.adapters.ui_queries import settings

                    current = settings(self.root)
                    if any(
                        v != current.get(k)
                        for k, v in document.items()
                        if k not in {"ui", "theme", "display"}
                    ):
                        invalid(
                            "실행 설정은 프로젝트 설정 화면에서 변경해주세요. 이 기능은 ui·theme·display만 변경합니다."
                        )
                    value = json.dumps(dict(current, **document), ensure_ascii=False)
                if key == "profile":
                    self._validate_profile(document)
                if op["id"] == "tool.register":
                    assert isinstance(document, dict)
                if op["id"] == "tool.register" and isinstance(document, dict) and (
                    document.get("enabled")
                    or document.get("authenticated")
                    or document.get("credential_ref")
                    or document.get("project_id") not in self.project_ids()
                ):
                    invalid(
                        "도구는 현재 프로젝트에 비활성 상태로 등록하고 연결 화면에서 인증해주세요."
                    )
                if materialize:
                    self._private(self.uploads)
                    path = self.uploads / (
                        request_id + "-" + key + (".md" if markdown else ".json")
                    )
                    if path.is_symlink():
                        invalid("입력 파일 경로를 확인해주세요.")
                    path.write_text(value, encoding="utf-8")
                    path.chmod(0o600)
                    value = str(path)
            elif field["type"] == "project-file":
                path = (self.workspace / value).resolve()
                if (
                    Path(value).is_absolute()
                    or ".." in Path(value).parts
                    or not path.is_relative_to(self.workspace)
                    or path.suffix.lower() != ".md"
                    or any(p.startswith(".") for p in Path(value).parts)
                    or not path.is_file()
                ):
                    invalid("현재 프로젝트의 커밋된 Markdown 문서 상대 경로를 입력해주세요.")
            elif key == "destination":
                value = str(self._artifact(value))
                if materialize:
                    self._private(self.artifacts)
            elif field["type"] == "artifact":
                value = str(self._artifact(value, existing=True))
            elif value.startswith("-"):
                invalid("입력은 옵션이 아닌 값을 사용해주세요.")
            prepared[key] = value
            positional.append(value)
        flags = []
        for key, value in options.items():
            if key in {"dry_run", "apply", "recover", "check_auth"}:
                if type(value) is not bool:
                    invalid("선택 옵션은 참·거짓이어야 합니다.")
                if value:
                    flags.append("--" + key.replace("_", "-"))
            else:
                if (
                    not isinstance(value, (str, int))
                    or isinstance(value, bool)
                    or len(str(value)) > 200
                ):
                    invalid("옵션 값을 확인해주세요.")
                flags.extend(["--" + key.replace("_", "-"), str(value)])
        # Notifications remain a dry-run query; no automatic or implicit external messaging.
        tokens = [op["namespace"]] + ([op["action"]] if op["action"] else []) + positional + flags
        try:
            args = parser().parse_args(tokens)
        except SystemExit:
            invalid("필드 형식과 옵션을 확인해주세요.")
        args.root, args.db, args.data = self.root, self.db_path, self.root / "data"
        args.actor, args.request_id = "local-web-user", request_id
        return op, args

    def project_ids(self):
        if not self.db_path.is_file():
            return []
        return [
            r["id"] for r in Database(self.db_path, read_only=True).rows("SELECT id FROM projects")
        ]

    def preview(self, payload):
        op, args = self.prepare(payload)
        result = {
            "operation": op["id"],
            "label": op["group"] + " · " + op["label"],
            "project": str(self.workspace),
            "actor": "local-web-user",
            "fields": redact(payload.get("fields", {}), self.secrets),
            "options": payload.get("options", {}),
            "mutates": op["mutates"],
            "preview_digest": digest(
                {k: v for k, v in payload.items() if k not in {"request_id", "preview_digest"}}
            ),
            "notes": ["기존 서비스의 승인·스냅샷·검증 조건이 그대로 적용됩니다."],
        }
        if op["namespace"] == "backup":
            from agent_dev.adapters.backup import EXCLUDED, physical_sources

            result["included"] = [
                kind
                for kind, path in physical_sources(self.root, self.workspace)[0].items()
                if path.is_file()
            ]
            result["excluded"] = EXCLUDED
            result["notes"].append(
                "포함: " + ", ".join(result["included"] + ["설정", "작업 근거", "출처·해시 검증 자료"])
            )
            result["notes"].append("제외: 소스, worktree, 설치 도구, 환경변수 값, OAuth·인증 비밀값")
            if op["id"] == "backup.restore":
                result["restore_check"] = True
                result["notes"].append(
                    "새 검사 폴더에만 복원합니다. 현재 프로젝트를 교체하지 않으며 결과 manifest를 내려받을 수 있습니다."
                )
        if hasattr(args, "task_id"):
            result["task"] = TaskQueries(self.root).explain(args.task_id)
        if op["id"] == "task.publish":
            result["notes"].append(
                "선택한 저장소에 게시합니다. 프로젝트 게시 권한과 검증 완료 조건이 필요합니다."
            )
        if op["id"] == "notification.drain":
            result["notes"].append("대기 건수만 조회합니다. 메시지는 발송하지 않습니다.")
        return result

    def context(self):
        from agent_dev.adapters.preparation import is_preparation
        from agent_dev.adapters.project_setup import ProjectSetup

        setup = ProjectSetup(self.root)
        _, _, profile = setup.current_profile()
        result = {
            "preparation_mode": is_preparation(self.root),
            "profile": redact(profile or setup.initial_profile(), self.secrets),
            "projects": [],
            "designs": [],
            "plans": [],
        }
        if self.db_path.is_file():
            db = Database(self.db_path, read_only=True)
            result["projects"] = db.rows("SELECT id,name FROM projects ORDER BY id LIMIT 100")
            result["designs"] = db.rows(
                "SELECT id,version,project_id FROM design_documents ORDER BY version DESC LIMIT 100"
            )
            result["plans"] = db.rows("SELECT id,project_id,state FROM task_plans LIMIT 100")
        return result

    def history(self, limit=50):
        if not self.db_path.is_file():
            return []
        db = Database(self.db_path, read_only=True)
        rows = db.rows(
            "SELECT commands.*,leases.holder AS lease_holder,leases.token AS lease_token,"
            "leases.expires_at AS lease_expires_at FROM commands "
            "LEFT JOIN leases ON leases.resource='agent-web:'||commands.key "
            "WHERE commands.key LIKE 'web:%' ORDER BY commands.created_at DESC LIMIT ?",
            (min(max(int(limit), 1), 100),),
        )
        result = []
        for row in rows:
            value = json.loads(row["result_json"] or "{}")
            if row["state"] in {"queued", "running"}:
                leases = (
                    [{
                        "holder": row["lease_holder"],
                        "token": row["lease_token"],
                        "expires_at": row["lease_expires_at"],
                    }]
                    if row["lease_holder"] is not None
                    else []
                )
                request = value.get("id")
                local = self.processes.get(request)
                shared = self.capture_progress.get(request)
                checkpoint = shared.value if shared is not None else 0
                # During capture the writer transaction's renewals are not yet visible
                # to readers. Only this server's fenced, live worker may supply proof.
                own_capture = bool(
                    leases
                    and local
                    and local[0].is_alive()
                    and checkpoint > 0
                    and row["state"] == "running"
                    and 0 <= time.monotonic() - checkpoint <= 30
                    and value.get("operation") == "backup.create"
                    and value.get("owner") == self.owner == leases[0]["holder"]
                    and local[1]["holder"] == leases[0]["holder"]
                    and local[1]["token"] == leases[0]["token"]
                    and local[1]["resource"] == "agent-web:" + row["key"]
                )
                if not own_capture and (not leases or leases[0]["expires_at"] <= time.time()):
                    value.update(
                        state="interrupted",
                        error="서버 또는 실행이 중단되었습니다. 원인과 작업 상태를 확인한 후 새 요청으로 재개해주세요.",
                    )
                else:
                    value["state"] = row["state"]
            else:
                value["state"] = row["state"]
            result.append(value)
        return redact(result, self.secrets)

    def start(self, payload):
        request = payload.get("request_id") or uuid.uuid4().hex
        if not isinstance(request, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request):
            invalid("요청 ID를 확인해주세요.")
        raw = {k: v for k, v in payload.items() if k not in {"request_id", "preview_digest"}}
        fingerprint = digest(raw)
        if payload.get("preview_digest") != fingerprint:
            invalid("입력을 미리 확인한 후 실행해주세요.")
        op, _ = self.prepare(payload)
        from agent_dev.adapters.preparation import AI_OPERATIONS, require_ai

        if op["id"] in AI_OPERATIONS:
            require_ai(self.root)
        key = "web:" + request
        with self.lock:
            store = WorkflowRepository(Database(self.db_path))
            with store.db.transaction() as connection:
                previous = connection.execute(
                    "SELECT * FROM commands WHERE key=?", (key,)
                ).fetchone()
                if previous:
                    if previous["request_digest"] != fingerprint:
                        invalid("같은 요청 ID에 다른 내용을 사용할 수 없습니다.")
                    return next(
                        (j for j in self.history(100) if j.get("id") == request),
                        json.loads(previous["result_json"]),
                    )
                op = commands()[raw["operation"]]
                target = raw["fields"].get("task_id", "")
                active = connection.execute(
                    "SELECT result_json FROM commands WHERE key LIKE 'web:%' AND state IN ('queued','running')"
                ).fetchall()
                live_count = 0
                for row in active:
                    job = json.loads(row[0])
                    live = connection.execute(
                        "SELECT expires_at FROM leases WHERE resource=?",
                        ("agent-web:web:" + job["id"],),
                    ).fetchone()
                    live_request = bool(live and live[0] > time.time()) or (
                        job.get("state") == "queued"
                        and job.get("created_at", 0) + 60 > time.time()
                        and not live
                    )
                    if live_request:
                        live_count += 1
                    if target and job.get("task_id") == target and live_request:
                        raise DomainError(
                            ErrorCode.LEASE_CONFLICT,
                            "이 작업의 요청이 이미 실행 중입니다. 실행 이력에서 취소하거나 완료를 기다려주세요.",
                        )
                if live_count >= 2:
                    raise DomainError(
                        ErrorCode.LEASE_CONFLICT,
                        "동시에 두 요청까지 실행할 수 있습니다. 실행 중인 요청 완료를 기다려주세요.",
                    )
                job = {
                    "id": request,
                    "operation": op["id"],
                    "task_id": target,
                    "state": "queued",
                    "created_at": time.time(),
                    "owner": self.owner,
                }
                connection.execute(
                    "INSERT INTO commands VALUES(?,?,?,?,?)",
                    (key, fingerprint, "queued", canonical(job), job["created_at"]),
                )
            lease = ExecutionSafety(store).acquire("agent-web:" + key, self.owner, ttl=60)
            try:
                _, args = self.prepare(payload, materialize=True, request_id=request)
                process_context = multiprocessing.get_context("spawn")
                # Single writer timestamp: no interprocess mutex can be stranded by cancellation.
                progress = (
                    process_context.Value("d", 0, lock=False)
                    if job["operation"] == "backup.create"
                    else None
                )
                process = process_context.Process(
                    target=_worker,
                    args=(
                        str(self.db_path),
                        key,
                        job,
                        args,
                        lease,
                        self.dispatcher,
                        os.getpid(),
                        progress,
                    ),
                    daemon=False,
                )
                process.start()
            except Exception:
                ExecutionSafety(store).release(lease)
                _record(
                    store,
                    key,
                    dict(job, state="interrupted", error="실행 프로세스를 시작하지 못했습니다."),
                )
                raise
            self.processes[request] = (process, lease)
            if progress is not None:
                self.capture_progress[request] = progress
            threading.Thread(target=self._monitor, args=(request, process, lease), daemon=True).start()
            return job

    def _monitor(self, request, process, lease):
        process.join()
        with self.lock:
            store = WorkflowRepository(Database(self.db_path))
            key = "web:" + request
            with store.db.transaction() as sql:
                row = sql.execute("SELECT state,result_json FROM commands WHERE key=?", (key,)).fetchone()
                if row and row["state"] in {"queued", "running"}:
                    job = json.loads(row["result_json"])
                    if job.get("owner") == self.owner:
                        job.update(state="interrupted", finished_at=time.time(), error="실행 프로세스가 중단되었습니다. 작업 상태를 확인한 뒤 새 요청으로 재개해주세요.")
                        sql.execute("UPDATE commands SET state='interrupted',result_json=? WHERE key=?",
                                    (canonical(job), key))
            ExecutionSafety(store).release(lease)

    def cancel(self, request):
        with self.lock:
            jobs = self.history(100)
            job = next((j for j in jobs if j["id"] == request), None)
            if not job:
                invalid("실행 ID를 확인해주세요.")
            entry = self.processes.get(request)
            if job["state"] not in {"queued", "running"}:
                return job
            if not entry:
                invalid("다른 서버의 실행입니다. 해당 서버에서 취소해주세요.")
            process, lease = entry
            process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            store = WorkflowRepository(Database(self.db_path))
            ExecutionSafety(store).release(lease)
            # The canonical task lease may remain fenced until its TTL expires.
            job.update(
                state="cancelled",
                finished_at=time.time(),
                error="실행 요청이 취소되었습니다. 작업 상태를 확인한 뒤 작업 취소 또는 재개를 선택해주세요.",
            )
            _record(store, "web:" + request, job)
            return job

    def close(self):
        for request, (process, _) in list(self.processes.items()):
            if process.is_alive():
                self.cancel(request)
            else:
                process.join(timeout=1)


def _record(store, key, job, lease=None):
    with store.db.transaction() as connection:
        if lease is not None:
            store.assert_lease(connection, lease)
        connection.execute(
            "UPDATE commands SET state=?,result_json=? WHERE key=?",
            (job["state"], canonical(redact(job, store.secrets)), key),
        )


def _worker(db_path, key, job, args, lease, dispatcher=None, parent_pid=None, capture_progress=None):
    from agent_dev.adapters.secrets import registered_secrets

    store = WorkflowRepository(Database(Path(db_path)), secrets=registered_secrets(args.root))
    safety = ExecutionSafety(store)
    stop = threading.Event()
    from agent_dev.adapters.backup_capture import WebBackupCapture

    capture = WebBackupCapture(safety, lease, capture_progress)

    def heartbeat():
        ticks = 0
        while not stop.wait(1):
            if parent_pid is not None and os.getppid() != parent_pid:
                if capture.active:
                    # Exit immediately; another connection cannot write through our lock.
                    os._exit(1)
                try:
                    _record(
                        store,
                        key,
                        dict(
                            job,
                            state="interrupted",
                            error="서버 연결이 종료되어 실행이 중단되었습니다.",
                        ),
                        lease,
                    )
                    safety.release(lease)
                finally:
                    os._exit(1)
            ticks += 1
            if ticks % 10 and not capture.active:
                continue
            try:
                capture.heartbeat()
            except Exception:  # noqa: BLE001 - lost fencing must stop every provider
                os._exit(1)

    keeper = threading.Thread(target=heartbeat, daemon=True)
    keeper.start()
    try:
        job.update(state="running", started_at=time.time())
        _record(store, key, job, lease)
        if args.namespace == "backup" and args.action == "create":
            # Server-owned identity never comes from the request payload or CLI flags.
            args._backup_self = {"request_id": job["id"], "lease": dict(lease)}
            args._backup_capture = capture.hold
        result = (dispatcher or dispatch)(args)
        from agent_dev.adapters.secrets import registered_secrets

        job.update(state="completed", result=redact(result, registered_secrets(args.root)))
    except Exception as exc:  # noqa: BLE001 - isolate and persist worker failures
        job.update(
            state="failed",
            error=str(exc)
            if isinstance(exc, DomainError)
            else "작업 실행에 실패했습니다. 입력과 환경 진단을 확인해주세요.",
            code=exc.code.value if isinstance(exc, DomainError) else "EXECUTION_FAILED",
        )
    finally:
        stop.set()
        keeper.join(timeout=1)
        job["finished_at"] = time.time()
        _record(store, key, job, lease)
        safety.release(lease)
