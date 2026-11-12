"""The devroster command parser and dispatcher (the only CLI since ADR 0003)."""

import argparse
import json
import sqlite3
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.migration.legacy import collect_sources, compare, import_sources
from agent_dev.migration.operations import Operations

NAMESPACES = {
    "refactor",
    "project",
    "design",
    "plan",
    "task",
    "knowledge",
    "tool",
    "view",
    "notification",
    "init",
    "doctor",
    "backup",
    "dashboard",
    "studio",
    "setup",
    "preset",
    "upgrade",
    "workflow",
    "integration",
    "figma",
    "stitch",
    "source",
    "changes",
    "skills",
    "rules",
    "ai",
    "settings",
    "feedback",
    "messenger",
    "worktree",
}


OVERVIEW = """DevRoster: 기획서를 바탕으로 구독 중인 AI 도구로 개발하는 로컬 에이전트

처음이라면:
  devroster app          웹 화면 실행 (설정 · Studio · 작업 실행을 화면에서)
  devroster doctor       설치·로그인·저장소 준비 상태 진단

터미널에서 자주 쓰는 명령:
  devroster task list    작업 목록과 상태
  devroster task run ID  승인된 작업 실행 (run · verify · resume · complete는 짧게도 사용)"""

# Help lines, in the order the command list shows them.
SUMMARIES = {
    "setup": "프로젝트 설정 (폴더 · AI 도구 · 검증 명령), --web으로 설정 화면",
    "init": "폴더를 DevRoster 작업 공간으로 초기화",
    "doctor": "설치 · 로그인 · 저장소 준비 상태 진단",
    "ai": "공통 AI 도구 설정 보기 · 저장 · 프로젝트 설정 가져오기",
    "skills": "역할별 스킬 묶음 제안 · 설치 · 동기화",
    "rules": "프로젝트 AI 규칙·스킬·훅(.ai/) 보기 · 동기화 · 훅 승인",
    "settings": "설정 · DB 저장 위치 확인과 이전",
    "worktree": "끝난 작업의 작업 폴더(워크트리) 확인 · 정리(--apply)",
    "upgrade": "작업 공간 저장소 업그레이드",
    "studio": "기획 · 명세 · 화면 검토(Studio) 웹 화면",
    "dashboard": "개발 현황 웹 화면",
    "project": "프로젝트 등록 · 프로필 조회",
    "design": "설계 문서 작성 · 승인",
    "plan": "작업 계획 작성 · 승인",
    "task": "작업 목록 · 실행 · 검증 · 게시 · 완료 · 사용량",
    "feedback": "완료한 작업의 피드백과 후속 계획",
    "changes": "요구사항 변경 미리보기 · 제안 · 적용",
    "workflow": "개발 흐름(역할 순서 · 단계) 조회 · 편집 · 적용 · 단계 승인",
    "knowledge": "위키 지식 제안 · 승인 · 폐기 · 열람본 생성",
    "source": "소스 탐색 · 검색 · 코드 그래프",
    "integration": "외부 도구(MCP) 카탈로그 · 연결 · 검사",
    "messenger": "슬랙 메신저 리스너 실행 · 상태 · 연결 검사",
    "tool": "도구 연결 등록과 일회성 권한",
    "figma": "Figma 화면 참고 자료",
    "stitch": "Stitch 화면 참고 자료",
    "view": "CLI 상태 보기와 보기 설정",
    "notification": "알림 대기열 처리",
    "backup": "작업 공간 백업 · 점검 · 복원",
    "preset": "검증 명령 프리셋 목록 · 내보내기",
    "refactor": "레거시 자료 가져오기 · DB 내보내기/복원 · 실행 플래그",
}


class _Parser(argparse.ArgumentParser):
    """Korean section title and help line; subcommands inherit the class."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)
        self._optionals.title = "옵션"
        self._positionals.title = "인수"
        self.add_argument("-h", "--help", action="help", help="이 도움말 보기")

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: {message}\n`{self.prog} --help`로 사용할 수 있는 명령을 확인하세요.\n")


# Web screens the CLI can start ("studio", "dashboard", "setup"). The ui layer sits outside
# adapters, so the entry point (bootstrap) registers them instead of this module importing ui.
WEB_SERVERS: dict = {}


def register_web_servers(servers) -> None:
    WEB_SERVERS.update(servers)


def web_server(name):
    serve = WEB_SERVERS.get(name)
    if serve is None:
        raise DomainError(
            ErrorCode.CAPABILITY_UNAVAILABLE, "웹 화면은 devroster 명령(설치된 실행 파일)으로 시작해주세요."
        )
    return serve


def handles(arguments):
    return bool(arguments) and arguments[0] in NAMESPACES


def parser():
    result = _Parser(
        prog="devroster",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=OVERVIEW,
        epilog="명령마다 `devroster <명령> --help`로 세부 동작을 볼 수 있습니다.",
    )
    spaces = result.add_subparsers(
        dest="namespace", required=True, title="명령", metavar="<명령>"
    )
    actions = {
        "workflow": {
            "presets": [],
            "list": [],
            "show": ["workflow_id"],
            "save": ["source"],
            "validate": ["source"],
            "preview": ["source"],
            "apply": ["workflow_id"],
            "export": ["workflow_id"],
            "import": ["source"],
            "approve": ["task_id", "node_id"],
            "state": ["task_id"],
        },
        "integration": {
            "catalog": [],
            "list": [],
            "show": ["connection_id"],
            "configure": ["source"],
            "check": ["connection_id"],
            "read": ["connection_id"],
            "doctor": [],
            "disable": ["connection_id"],
            "launch": ["connection_id"],
            "export": [],
            "import": ["source"],
            "prepare": ["provider"],
            "auth": ["connection_id"],
        },
        "figma": {"check": [], "inspect": ["locator"], "reference": ["locator"], "cache": []},
        "stitch": {
            "check": [],
            "projects": [],
            "screens": [],
            "inspect": ["locator"],
            "reference": ["locator"],
            "cache": [],
        },
        "source": {
            "modules": [],
            "tree": [],
            "file": [],
            "search": ["query"],
            "impact": [],
            "graph": [],
            "build": [],
        },
        "refactor": {
            "migrate": [],
            "compare": [],
            "export": ["destination"],
            "restore": ["source", "destination"],
            "rollback-drill": [],
            "rollback": [],
            "observe-release": ["release_id"],
            "status": [],
            "retention": [],
            "configure": ["mode"],
        },
        "project": {
            "register": ["project_id", "profile"],
            "profile": ["project_id", "profile"],
            "show": ["project_id"],
        },
        "design": {
            "refine": ["project_id", "design_id", "source"],
            "approve": ["design_id", "version"],
        },
        "plan": {
            "create": ["project_id", "plan_id", "design_id", "version", "source"],
            "approve": ["plan_id"],
        },
        "task": {
            "list": [],
            "explain": ["task_id"],
            "usage": ["task_id"],
            "show": ["task_id"],
            "run": ["task_id"],
            "resume": ["task_id"],
            "verify": ["task_id"],
            "publish": ["task_id", "repository"],
            "review": ["task_id"],
            "complete": ["task_id", "publish_id"],
            "cancel": ["task_id"],
            "checkpoint": ["task_id"],
            "quick": ["module", "title", "source"],
        },
        "feedback": {
            "add": ["task_id", "source"],
            "plan": ["feedback_id", "plan_id", "design_id", "version"],
        },
        "knowledge": {
            "candidate": ["project_id", "source", "commit"],
            "confirm": ["candidate_id", "evidence_id"],
            "search": ["project_id", "query"],
            "deprecate": ["candidate_id"],
            "propose": ["project_id", "source"],
            "revise": ["project_id", "item_id", "source"],
            "approve": ["project_id", "item_id", "revision"],
            "reject": ["project_id", "item_id", "revision"],
            "retire": ["project_id", "item_id", "reason"],
            "list": ["project_id"],
            "render": ["project_id"],
        },
        "tool": {
            "register": ["source"],
            "grant": ["connection_id", "capability", "scope", "task_id", "phase_id"],
        },
        "view": {"snapshot": [], "settings": [], "set-settings": ["source"]},
        "notification": {"drain": ["project_id"]},
        "backup": {
            "create": ["destination"],
            "inspect": ["source"],
            "restore": ["source", "destination"],
        },
        "preset": {"list": [], "show": ["preset"], "write": ["preset", "destination"]},
        "upgrade": {"": []},
        "init": {"": []},
        "doctor": {"": []},
        "dashboard": {"": []},
        "studio": {"": []},
        "setup": {"": []},
        "settings": {"location": ["operation", "target?"]},
        "messenger": {"listen": [], "status": [], "test": []},
        "skills": {"catalog": [], "suggest": [], "status": [], "sync": []},
        "rules": {"show": [], "sync": [], "check": [], "init": [], "hooks": [], "approve-hooks": ["digest"]},
        "worktree": {"list": [], "prune": []},
        "ai": {"show": [], "set": ["source"], "import": []},
        "changes": {
            "bootstrap": [],
            "query": [],
            "detail": ["change_id"],
            "preview": ["project_id", "source_import_id", "reason"],
            "propose": ["project_id", "source_import_id", "preview_digest", "reason"],
            "apply": ["change_id", "expected_version"],
            "reject": ["change_id", "expected_version", "reason"],
            "guard": ["task_id"],
        },
    }
    for name, summary in SUMMARIES.items():
        commands = actions[name]
        listed = "" if "" in commands else "\n\n동작: " + ", ".join(commands)
        space = spaces.add_parser(
            name,
            help=summary,
            description=summary + listed,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        child = (
            space.add_subparsers(
                dest="action", required=True, metavar="<동작>", help="위 동작 목록 중 하나"
            )
            if "" not in commands
            else None
        )
        for action, arguments in commands.items():
            command = child.add_parser(action) if child else space
            if not child:
                command.set_defaults(action="")
            for argument in arguments:
                # A trailing "?" marks an optional positional argument.
                command.add_argument(
                    argument.rstrip("?"), nargs="?" if argument.endswith("?") else None
                )
            command.add_argument("--root", type=Path)
            if name not in {
                "init",
                "doctor",
                "backup",
                "dashboard",
                "studio",
                "setup",
                "settings",
                "preset",
                "upgrade",
            }:
                command.add_argument("--db", type=Path)
                command.add_argument("--data", type=Path)
            else:
                command.set_defaults(db=None, data=None)
            command.add_argument("--actor", default="local-user")
            command.add_argument("--request-id", default="")
            command.add_argument("--json", action="store_true")
            if name not in {
                "init",
                "doctor",
                "backup",
                "dashboard",
                "studio",
                "setup",
                "settings",
                "preset",
                "upgrade",
            }:
                command.add_argument("--dry-run", action="store_true")
                command.add_argument("--apply", action="store_true")
            else:
                command.set_defaults(dry_run=False, apply=False)
            if name in {"workflow", "integration", "figma", "stitch", "source"}:
                command.add_argument("--expected-version", type=int, default=0)
                command.add_argument("--version", type=int)
                command.add_argument("--project", default="")
                command.add_argument("--scope", default="")
                command.add_argument("--operation", default="")
                command.add_argument("--arguments", type=Path)
                command.add_argument("--remap", type=Path)
                command.add_argument("--connection", default="")
                command.add_argument("--screen", default="")
                command.add_argument(
                    "--decision", choices=["approved", "rejected"], default="approved"
                )
                command.add_argument("--module", default="source")
                command.add_argument(
                    "--confirm-digest",
                    default="",
                    help="integration launch: 확인한 실행 명령의 digest (보여준 값 그대로)",
                )
                command.add_argument("--path", default="")
            if name == "source":
                command.add_argument("--mode", choices=["filename", "content"], default="filename")
                command.add_argument("--case", action="store_true", help="대소문자 구분")
                command.add_argument("--cursor", default="", help="이전 결과의 next_cursor")
            if name == "init":
                command.add_argument("--name", default="")
                command.add_argument("--project-id", default="")
                command.add_argument(
                    "--runner",
                    choices=["codex_cli", "claude_cli", "gemini_cli"],
                    default="codex_cli",
                )
                command.add_argument("--allow-path", action="append", default=[])
                command.add_argument(
                    "--preset",
                    choices=["generic", "python", "react", "spring-boot", "spring-boot-gradle", "auto"],
                    default="generic",
                )
            if name == "worktree":
                command.add_argument("--task", default="", help="이 작업만")
            if name == "upgrade":
                command.add_argument("--apply", action="store_true")
                command.add_argument("--recover", action="store_true")
                command.add_argument("--backup", type=Path)
            if name == "doctor":
                command.add_argument("--check-auth", action="store_true")
                command.add_argument(
                    "--fix-permissions", action="store_true", help="인증 파일 권한을 0600/0700으로 되돌림"
                )
            if name == "setup":
                mode = command.add_mutually_exclusive_group()
                mode.add_argument("--web", action="store_true")
                mode.add_argument("--show", action="store_true")
                mode.add_argument("--check", action="store_true")
                mode.add_argument("--from", dest="source", type=Path)
                mode.add_argument(
                    "--preview-task", help="Preview model/effort selection without calling AI"
                )
                command.add_argument("--preview-phase", default="developer")
                command.add_argument("--check-auth", action="store_true")
                command.add_argument("--port", type=int)
                command.add_argument(
                    "--copy",
                    action="store_true",
                    help="Copy imported settings to the target project without copying credentials",
                )
            if name in {"dashboard", "studio"}:
                command.add_argument("--port", type=int)
            if name == "ai" and action == "set":
                command.add_argument(
                    "--expected-revision",
                    type=int,
                    help="AI tools revision this file was based on (default: the latest)",
                )
            if name == "settings":
                command.add_argument(
                    "--scope",
                    choices=["workspace", "management"],
                    help="plan: move this workspace's settings or the whole management folder",
                )
                command.add_argument(
                    "--action",
                    dest="recover_action",
                    choices=["resume", "rollback"],
                    help="recover: finish an interrupted move or reverse it when still safe",
                )
                command.add_argument(
                    "--expected-revision",
                    type=int,
                    help="apply: registry revision seen by the plan (default: the plan's)",
                )
            if name == "changes":
                if action in {"preview", "propose"}:
                    command.add_argument("--selection", type=Path, help="Structured preview selection JSON, including handoff tasks and impact decisions")
                    command.add_argument("--new-studio-id", default="")
                if action in {"bootstrap", "query", "detail", "apply", "reject"}:
                    command.add_argument("--project-id", default="")
                if action in {"bootstrap", "query"}:
                    command.add_argument("--limit", type=int, default=100)
                    command.add_argument("--offset", type=int, default=0)
                    command.add_argument("--state", default="")
            if name == "task" and action == "complete":
                command.add_argument(
                    "--keep-worktree", action="store_true", help="완료 후에도 작업 폴더(worktree)를 남김"
                )
            if name == "task" and action == "quick":
                command.add_argument("--project", default="", help="프로젝트가 여러 개일 때")
            if name == "task" and action in {"list", "explain"}:
                command.add_argument("--limit", type=int, default=100)
                if action == "list":
                    command.add_argument("--search", default="")
                    command.add_argument("--state", default="")
                    command.add_argument("--project", default="")
                    command.add_argument("--page", type=int, default=1)
    return result


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rollback_drill(runtime):
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        export = Path(folder) / "backup.db"
        restored = Path(folder) / "restore.db"
        operations = Operations(runtime.store)
        operations.export(export)
        operations.restore(export, restored)
        other = Database(restored)
        tables = [
            r["name"]
            for r in runtime.store.db.rows("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        equal = all(
            runtime.store.db.rows(f'SELECT * FROM "{table}"')
            == other.rows(f'SELECT * FROM "{table}"')
            for table in tables
        )
        return {"passed": equal, "tables": len(tables)}


WIKI_ACTIONS = {"propose", "revise", "approve", "reject", "retire", "list", "render"}


def wiki_knowledge(wiki, args, workspace):
    """Revisioned wiki items; entries come from a JSON file (one object or a list)."""
    action = args.action
    if action in {"propose", "revise"}:
        try:
            entries = json.loads((workspace / args.source).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DomainError(ErrorCode.INVALID_INPUT, f"Unreadable knowledge file: {error}")
        if action == "propose":
            return {"proposed": wiki.propose(args.project_id, entries)}
        return wiki.revise(args.project_id, args.item_id, entries)
    if action in {"approve", "reject"}:
        try:
            revision = int(args.revision)
        except ValueError:
            raise DomainError(ErrorCode.INVALID_INPUT, "Revision must be a number.")
        method = wiki.approve if action == "approve" else wiki.reject
        return method(args.project_id, args.item_id, revision, args.actor)
    if action == "retire":
        return wiki.retire(args.project_id, args.item_id, args.reason, args.actor)
    if action == "list":
        return {"items": wiki.items(args.project_id)}
    return wiki.render(args.project_id)


def _rules(root, action, args=None):
    from agent_dev.adapters import ai_hooks, ai_rules

    target = root  # A plain repository: the rules live right here.
    if (root / ".agent-dev").is_dir() or (root / "workspace.json").is_file():
        from agent_dev.adapters.workflow_runtime import project_root

        target = project_root(root)  # A workspace: rules live with its source folder.
    if action == "init":
        return ai_rules.init(target)
    if action in {"hooks", "approve-hooks"}:
        config = ai_hooks._config(root)
        if action == "approve-hooks":
            # The digest printed by `rules hooks`: approving means "exactly what I reviewed".
            ai_hooks.approve(config, target, getattr(args, "digest", ""))
        return ai_hooks.overview(target, config)
    if action == "show":
        return ai_rules.overview(target)
    result = ai_rules.sync(target, check=action == "check")
    if not result["ok"]:
        raise DomainError(
            ErrorCode.VERIFICATION_FAILED,
            "최상위 AI 파일이 .ai/rules와 다르거나 표시 영역이 깨졌습니다: "
            + ", ".join(result["stale"])
            + (". devroster rules sync로 다시 만드세요." if action == "check" else ""),
        )
    return result


# Commands that actually preview with --dry-run. Every other command would run for real.
DRY_RUN_COMMANDS = frozenset(
    {("refactor", "migrate"), ("refactor", "retention"), ("task", "publish"), ("skills", "sync")}
)


def dispatch(args):
    root = args.root.resolve()
    from agent_dev.adapters.secrets import registered_secrets

    secrets = registered_secrets(root)
    data = args.data or root / "data"
    from agent_dev.adapters.workflow_runtime import project_root
    from agent_dev.adapters.workspace_locator import workspace_locations

    workspace = project_root(root)
    db = args.db or workspace_locations(root).state_db
    namespace, action = args.namespace, args.action
    if namespace in {"workflow", "integration", "figma", "stitch", "source"}:
        from agent_dev.adapters.feature_cli import run

        return run(args)
    if namespace == "setup":
        from agent_dev.adapters.setup_cli import run

        return run(args)
    if namespace == "messenger":
        from agent_dev.adapters import messenger

        if action == "status":
            return messenger.status(root)
        if action == "test":
            return messenger.check(root)
        listener = messenger.MessengerListener(root)
        print("슬랙 리스너 실행 중입니다. Ctrl+C로 종료합니다.", file=sys.stderr, flush=True)
        try:
            listener.run()
        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()
        return messenger.status(root, listener)
    if namespace == "settings":
        from agent_dev.adapters.setup_cli import settings_location

        return settings_location(args)
    if namespace == "ai":
        from agent_dev.adapters.ai_tools_service import run as ai_tools

        return ai_tools(args)
    if namespace == "skills":
        from agent_dev.adapters.skill_cli import run as skills

        return skills(args)
    if namespace == "rules":
        return _rules(root, action, args)
    if namespace == "init":
        from agent_dev.adapters.workspace import initialize

        return initialize(
            root,
            name=args.name,
            project_id=args.project_id,
            runner=args.runner,
            paths=args.allow_path,
            preset=args.preset,
        )
    if namespace == "preset":
        from agent_dev.adapters.presets import listing, profile, write

        if action == "list":
            return listing()
        if action == "show":
            return profile(args.preset)
        return write(args.preset, args.destination)
    if namespace == "upgrade":
        from agent_dev.adapters.upgrades import recover, upgrade

        if args.recover and args.apply:
            raise DomainError(ErrorCode.INVALID_INPUT, "Choose --recover or --apply.")
        return (
            recover(root) if args.recover else upgrade(root, apply=args.apply, backup=args.backup)
        )
    if namespace == "doctor":
        from agent_dev.adapters.diagnostics import diagnose

        return diagnose(root, check_auth=args.check_auth, fix_permissions=args.fix_permissions)
    if namespace == "backup":
        from agent_dev.adapters.backup import create_bundle, inspect_bundle, restore_bundle

        if action == "create":
            return create_bundle(
                root,
                Path(args.destination),
                self_request=getattr(args, "_backup_self", None),
                capture_guard=getattr(args, "_backup_capture", None),
            )
        if action == "inspect":
            return inspect_bundle(Path(args.source))
        return restore_bundle(Path(args.source), Path(args.destination))
    if namespace == "studio":
        from agent_dev.adapters.project_setup import ProjectSetup

        return web_server("studio")(
            root,
            args.port if args.port is not None else ProjectSetup(root).read()["ports"]["studio"],
        )
    if namespace == "dashboard":
        from agent_dev.adapters.project_setup import ProjectSetup

        return web_server("dashboard")(
            root,
            args.port if args.port is not None else ProjectSetup(root).read()["ports"]["dashboard"],
        )
    if namespace == "task" and action in {"list", "explain", "show", "usage"}:
        from agent_dev.adapters.task_queries import TaskQueries

        query = TaskQueries(root, db)
        if action == "list":
            return query.tasks(
                args.limit,
                search=args.search,
                state=args.state,
                project=args.project,
                page=args.page,
            )
        if action == "explain":
            return query.explain(args.task_id, args.limit)
        return query.usage(args.task_id) if action == "usage" else query.show(args.task_id)
    if namespace == "project" and action == "show":
        if not db.is_file():
            return []
        return Database(db, read_only=True).rows(
            "SELECT * FROM projects WHERE id=?", (args.project_id,)
        )
    if namespace == "view":
        from agent_dev.adapters.ui_queries import settings, snapshot, update_settings

        if action == "set-settings":
            return update_settings(root, args.source)
        return snapshot(root) if action == "snapshot" else settings(root)
    if namespace == "refactor" and action == "restore":
        return Operations.restore(args.source, args.destination)
    if namespace == "refactor" and action == "migrate" and args.dry_run:
        sources = collect_sources(workspace, data, secrets)
        # No connection, schema migration, output directory or backup in dry-run.
        return import_sources(None, sources, dry_run=True)
    if namespace == "refactor" and action == "compare" and not db.exists():
        raise DomainError(ErrorCode.INVALID_INPUT, "No shadow database. Run migrate first.")
    runtime = WorkflowRuntime(root, db)
    store, governance = runtime.store, runtime.governance
    if namespace == "worktree":
        from agent_dev.adapters.worktrees import cleanup

        # list never changes anything; prune previews unless --apply is given.
        return cleanup(store, runtime.safety, args.task, apply=action == "prune" and args.apply)
    operations = Operations(store)
    if namespace == "refactor":
        sources = (
            collect_sources(workspace, data, secrets)
            if action in {"migrate", "compare", "observe-release", "configure"}
            else {}
        )
        if action == "migrate":
            backup = db.with_name(f"pre-import-{uuid.uuid4().hex}.db")
            operations.export(backup)
            return import_sources(store, sources, dry_run=False)
        if action == "compare":
            return compare(store, sources)
        if action == "export":
            return operations.export(args.destination)
        if action == "rollback":
            return operations.rollback(root)
        if action == "rollback-drill":
            return rollback_drill(runtime)
        if action == "observe-release":
            from agent_dev.adapters.worktrees import git

            commit = git(root, "rev-parse", "--verify", f"refs/tags/{args.release_id}^{{commit}}")
            operations.observe_release(commit, compare(store, sources), rollback_drill(runtime))
            return {
                "release_id": args.release_id,
                "commit": commit,
                "retirement_ready": operations.retirement_ready(),
            }
        if action == "status":
            return {
                "flags": runtime.flags(),
                "retirement_ready": operations.retirement_ready(),
                "task_count": store.db.rows("SELECT COUNT(*) AS n FROM tasks")[0]["n"],
            }
        if action == "retention":
            return operations.retention(workspace / "output", apply=args.apply)
        if action == "configure":
            modes = {"off", "shadow", "dual-write", "read", "execute", "publish", "legacy-readonly"}
            if args.mode not in modes:
                raise DomainError(ErrorCode.INVALID_INPUT, "Unknown migration mode.")
            if args.mode not in {"off", "shadow"} and not compare(store, sources)["zero_diff"]:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Migration comparison must be zero-diff."
                )
            if args.mode == "legacy-readonly" and not operations.retirement_ready():
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "Two distinct tagged releases and rollback drills required.",
                )
            flags = {
                "read": args.mode in {"read", "execute", "publish", "legacy-readonly"},
                "dual_write": args.mode in {"dual-write", "read", "execute", "publish"},
                "execute": args.mode in {"execute", "publish", "legacy-readonly"},
                "publish": args.mode == "publish",
                "legacy_readonly": args.mode == "legacy-readonly",
            }
            path = root / "config/runtime.json"
            config = read_json(path) if path.exists() else {}
            config["orchestrator_v2"] = flags
            path.parent.mkdir(parents=True, exist_ok=True)
            from agent_dev.core.io import write_json

            write_json(path, config)
            return flags
    if namespace == "project":
        if action == "register":
            store.register_project(args.project_id, args.project_id, read_json(args.profile))
            return {"project_id": args.project_id}
        if action == "profile":
            row = store.db.rows("SELECT version FROM projects WHERE id=?", (args.project_id,))
            if not row:
                raise DomainError(ErrorCode.INVALID_INPUT, "Unknown project; register it first.")
            version = store.revise_profile(
                args.project_id, read_json(args.profile), row[0]["version"]
            )
            return {
                "project_id": args.project_id,
                "profile_version": version,
                "existing_snapshots_unchanged": True,
            }
        return store.db.rows("SELECT * FROM projects WHERE id=?", (args.project_id,))
    if namespace == "design":
        if action == "refine":
            from agent_dev.application.design import DesignAssessment

            content = Path(args.source).read_text(encoding="utf-8")
            assessment = DesignAssessment().assess(content)
            version = store.design(args.project_id, args.design_id, content)
            return {
                "design_id": args.design_id,
                "version": version,
                "state": "user_review",
                "sufficient": assessment.sufficient,
                "missing": assessment.missing,
                "question": assessment.question,
            }
        return {
            "approval_id": store.approve("design", args.design_id, int(args.version), args.actor)
        }
    if namespace == "plan":
        if action == "create":
            store.create_plan(
                args.project_id,
                args.plan_id,
                args.design_id,
                int(args.version),
                read_json(args.source),
            )
            return {"plan_id": args.plan_id, "state": "approval_pending"}
        plan = store.db.rows("SELECT * FROM task_plans WHERE id=?", (args.plan_id,))
        if not plan:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown plan.")
        store.approve("plan", args.plan_id, plan[0]["version"], args.actor)
        ids = [
            r["id"] for r in store.db.rows("SELECT id FROM tasks WHERE plan_id=?", (args.plan_id,))
        ]
        return {
            "plan_id": args.plan_id,
            "snapshots": {
                task_id: store.create_snapshot(task_id, runtime.resolve_snapshot(task_id))
                for task_id in ids
            },
        }
    if namespace == "task":
        if action == "show":
            return asdict(store.task(args.task_id))
        if action == "quick":
            from agent_dev.adapters import quick_fix

            description = Path(args.source).read_text(encoding="utf-8")
            if args.dry_run:
                plan = quick_fix.preview(store, args.module, args.title, description, args.project)
                return {k: v for k, v in plan.items() if k not in {"graph", "scope"}}
            from agent_dev.adapters.preparation import require_ai

            require_ai(root)  # Refuse before anything is created when AI runs are not allowed.
            created = quick_fix.create(runtime, args.module, args.title, description, args.actor, args.project)
            request_id = args.request_id or uuid.uuid4().hex
            run = runtime.execute(
                created["task_id"],
                request_id + ":run",
                tools=__import__("agent_dev.adapters.integrations", fromlist=["Integrations"])
                .Integrations(root)
                .call,
            )
            return dict(created, run=run)
        if action in {"run", "resume", "verify"}:
            return runtime.execute(
                args.task_id,
                args.request_id or uuid.uuid4().hex,
                verify=action == "verify",
                tools=__import__("agent_dev.adapters.integrations", fromlist=["Integrations"])
                .Integrations(root)
                .call,
            )
        if action == "checkpoint":
            with runtime.safety.lease(f"task:{args.task_id}") as lease:
                return {"path": runtime.artifacts.checkpoint(args.task_id, lease)}
        if action == "cancel":
            from agent_dev.domain.status import TaskStatus
            from agent_dev.domain.transitions import TransitionContext

            with runtime.safety.lease(f"task:{args.task_id}") as lease:
                task = store.task(args.task_id)
                store.transition(
                    args.task_id,
                    TaskStatus.CANCELLED,
                    TransitionContext(expected_version=task.version),
                    lease,
                )
                from agent_dev.adapters.worktrees import WorktreeManager

                try:
                    bindings = store.snapshot(args.task_id)["resolved"]["repositories"]
                except DomainError:
                    bindings = {}  # Never approved: no worktrees were made.
                manager = WorktreeManager(runtime.locations.worktree_root, runtime.safety)
                return {
                    "task_id": args.task_id,
                    "state": "cancelled",
                    "worktrees": {name: manager.release(row) for name, row in bindings.items()},
                }
        from agent_dev.adapters.publishing import PublishAdapter

        publishing = PublishAdapter(runtime)
        if action == "review":
            return publishing.review(args.task_id)
        if action == "publish":
            return publishing.publish(
                args.task_id,
                args.repository,
                args.request_id or uuid.uuid4().hex,
                dry_run=args.dry_run,
            )
        return publishing.complete(
            args.task_id, args.publish_id, args.actor, clean_worktree=not args.keep_worktree
        )
    if namespace == "feedback":
        if action == "add":
            return {
                "feedback_id": governance.feedback(
                    args.task_id, Path(args.source).read_text(encoding="utf-8")
                )
            }
        return governance.plan_feedback(
            args.feedback_id, args.plan_id, args.design_id, int(args.version)
        )
    if namespace == "knowledge" and action in WIKI_ACTIONS:
        return wiki_knowledge(runtime.wiki, args, workspace)
    if namespace == "knowledge":
        if action == "candidate":
            return {
                "candidate_id": governance.knowledge_candidate(
                    args.project_id,
                    args.source,
                    args.commit,
                    (workspace / args.source).read_text(encoding="utf-8"),
                )
            }
        if action == "confirm":
            governance.confirm_knowledge(args.candidate_id, args.evidence_id)
        elif action == "search":
            return governance.search_knowledge(args.project_id, args.query)
        else:
            governance.deprecate_knowledge(args.candidate_id)
        return {"state": action}
    if namespace == "notification":
        rows = store.db.rows(
            "SELECT content_json FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (args.project_id,),
        )
        if not rows:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown notification project.")
        policy = json.loads(rows[0]["content_json"]).get("notification", {})
        if not args.apply:
            return {
                "dry_run": True,
                "pending": store.db.rows(
                    "SELECT COUNT(*) AS n FROM notification_outbox WHERE state='pending'"
                )[0]["n"],
            }
        from agent_dev.adapters.notifications import DiscordOutboxSender
        from agent_dev.adapters.tool_gateway import ToolGateway

        connection = ToolGateway(store).connection(policy.get("connection_id", ""))
        channel = policy.get("channel_id", "")
        if (
            connection.project_id != args.project_id
            or not connection.enabled
            or not connection.authenticated
            or not connection.healthy
            or "notification.send" not in connection.capabilities
            or channel not in connection.allowlist
        ):
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Notification connection and channel grant required."
            )
        sender = DiscordOutboxSender(channel, connection.credential_ref)

        def scoped_sender(key, payload):
            belongs = payload.get("project_id") == args.project_id
            if belongs and payload.get("kind") not in {
                "ToolCall",
                "ToolRegistered",
                "ToolGrantIssued",
            }:
                sender(key, payload)

        return runtime.safety.deliver(scoped_sender, project_id=args.project_id)
    if namespace == "tool":
        from agent_dev.adapters.tool_gateway import ToolGateway
        from agent_dev.domain.contracts import ToolConnection

        gateway = ToolGateway(store)
        if action == "register":
            value = read_json(args.source)
            value["capabilities"] = frozenset(value["capabilities"])
            value["allowlist"] = tuple(value.get("allowlist", []))
            gateway.register(ToolConnection(**value))
            return {"connection_id": value["connection_id"]}
        return {
            "approval_id": gateway.grant(
                args.connection_id,
                args.capability,
                args.scope,
                args.actor,
                task_id=args.task_id,
                phase_id=args.phase_id,
            )
        }
    if namespace == "changes":
        from agent_dev.adapters import requirement_changes as rc

        db_path = db
        read_only = action in {"bootstrap", "query", "detail", "guard"}
        cdb = Database(db_path, read_only=read_only)
        if action == "bootstrap":
            return rc.bootstrap(cdb, {"project_id": args.project_id, "limit": args.limit, "offset": args.offset}, root)
        if action == "query":
            return rc.query(cdb, {"project_id": args.project_id, "state": args.state, "limit": args.limit, "offset": args.offset})
        if action == "detail":
            return rc.detail(cdb, args.change_id, args.project_id or None)
        if action in {"preview", "propose"}:
            selection = read_json(args.selection) if args.selection else {}
            selection.update(project_id=args.project_id, source_import_id=args.source_import_id, reason=args.reason, actor=args.actor)
            if args.new_studio_id:
                selection["new_studio_id"] = args.new_studio_id
            if action == "preview":
                return rc.preview(cdb, root, selection)
            return rc.propose(cdb, root, dict(selection, preview_digest=args.preview_digest, request_id=args.request_id))
        if action == "apply":
            return rc.apply(cdb, root, {"project_id": args.project_id, "change_id": args.change_id, "expected_version": int(args.expected_version), "request_id": args.request_id})
        if action == "reject":
            return rc.reject(cdb, {"project_id": args.project_id, "change_id": args.change_id, "expected_version": int(args.expected_version), "reason": args.reason, "actor": args.actor})
        if action == "guard":
            return rc.execution_guard(cdb, args.task_id)
    raise DomainError(ErrorCode.INVALID_INPUT, "Unknown command.")


def main(arguments):
    args, envelope, rejected = execute(arguments)
    print(
        json.dumps(
            envelope if args.json else envelope.get("result", envelope),
            ensure_ascii=False,
            indent=2,
        )
    )
    if rejected:
        raise SystemExit(1)


def execute(arguments):
    """Run one command; ``(args, redacted envelope, rejected)`` without printing."""
    args = parser().parse_args(arguments)
    request_id = args.request_id or uuid.uuid4().hex
    args.request_id = request_id
    try:
        if getattr(args, "dry_run", False) and (
            (args.namespace, args.action) not in DRY_RUN_COMMANDS or getattr(args, "apply", False)
        ):
            raise DomainError(
                ErrorCode.INVALID_INPUT,
                f"`{args.namespace} {args.action}` cannot preview with --dry-run"
                + (" together with --apply" if getattr(args, "apply", False) else "")
                + "; nothing was changed.",
            )
        from agent_dev.adapters.workspace import configuration_root, default_root, discover

        selected_root = (
            discover() or Path.cwd()
            if args.namespace == "setup"
            else Path.cwd()
            if args.namespace == "init"
            else default_root()
        )

        args.root = Path(args.root or selected_root).expanduser().resolve()
        if args.namespace != "init":
            args.root = configuration_root(args.root)
        mutating = (
            args.namespace in {"project", "design", "plan", "task", "feedback", "tool", "knowledge"}
            and args.action not in {"show", "search", "list", "explain", "usage"}
            and not args.dry_run
        )
        if mutating:
            from agent_dev.domain.content import content_digest

            request = {
                key: str(value) if isinstance(value, Path) else value
                for key, value in vars(args).items()
                if key != "request_id"
            }
            for field in ("source", "profile"):
                value = getattr(args, field, None)
                if value and Path(value).is_file():
                    request[field + "_digest"] = content_digest(Path(value).read_bytes())
            runtime = WorkflowRuntime(args.root, args.db)
            result = runtime.safety.command(
                f"{args.namespace}:{args.action}:{request_id}", request, lambda: dispatch(args)
            )
        else:
            result = dispatch(args)
        comparison = result.get("comparison") if isinstance(result, dict) else None
        rejected = isinstance(result, dict) and (
            result.get("zero_diff") is False
            or result.get("passed") is False
            or result.get("healthy") is False
            or (isinstance(comparison, dict) and comparison.get("zero_diff") is False)
            or result.get("state") == "blocked"
        )
        envelope = {
            "request_id": request_id,
            "status": "blocked"
            if rejected
            else "accepted"
            if isinstance(result, dict) and result.get("state") == "waiting_for_capacity"
            else "completed",
            "aggregate_id": getattr(args, "task_id", getattr(args, "project_id", "")),
            "aggregate_version": result.get("version") if isinstance(result, dict) else None,
            "artifacts": [],
            "operator_actions": result.get("operator_actions", [])
            if isinstance(result, dict)
            else [],
            "error": (
                {
                    "code": result.get("error", "VERIFICATION_FAILED"),
                    "message": result.get(
                        "reason",
                        "Environment checks failed; inspect checks and fixes."
                        if args.namespace == "doctor"
                        else "Required acceptance gate was not satisfied.",
                    ),
                }
                if isinstance(result, dict) and (rejected or result.get("error"))
                else None
            ),
            "result": result,
        }
    except (DomainError, ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        code = exc.code.value if isinstance(exc, DomainError) else "INVALID_INPUT"
        envelope = {
            "request_id": request_id,
            "status": "rejected",
            "aggregate_id": "",
            "aggregate_version": None,
            "artifacts": [],
            "operator_actions": [],
            "error": {
                "code": code,
                "message": str(exc) if isinstance(exc, DomainError) else "Invalid command input.",
            },
        }
        rejected = True
    from agent_dev.adapters.secrets import registered_secrets
    from agent_dev.adapters.workspace import default_root
    from agent_dev.domain.content import redact

    # A rejection before root resolution (e.g. --dry-run on run/resume) has no root yet.
    envelope = redact(envelope, registered_secrets(args.root or default_root()))
    return args, envelope, rejected
