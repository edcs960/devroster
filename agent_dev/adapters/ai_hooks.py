"""Project hooks in `.ai/hooks/hooks.json`, run by DevRoster itself for every AI tool.

A hook is a command the project wants run at a DevRoster moment (after code edits, before
verification, before publishing, ...). DevRoster runs it, not the AI tool, so it behaves the
same whether Claude, Codex, Gemini or an API model did the work. Three moments block on
failure (after_edit, before_verify, before_publish); the rest only record their result.

A repository can carry anyone's hooks, so nothing runs until the local user approves the
exact content: the digest of the whole `.ai/hooks` folder (hooks.json and its scripts). A
changed folder needs a new approval before a task can be approved, and a folder that changes
after approval does not run.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError, ErrorCode

HOOKS_DIR = ".ai/hooks"
HOOKS_FILE = ".ai/hooks/hooks.json"
APPROVAL_FILE = "ai-hooks-approval.json"
# Moment -> "block" (failure stops that step) or "record" (failure is only recorded).
EVENTS = {
    "after_edit": "block",
    "before_verify": "block",
    "before_publish": "block",
    "after_verify": "record",
    "after_publish": "record",
    "on_problem": "record",
    "phase_start": "record",
    "phase_end": "record",
    "studio_confirmed": "record",
}
MAX_FILE_BYTES = 64 * 1024
MAX_HOOKS_PER_EVENT = 10
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600
OUTPUT_TAIL = 4000
# Only what a build tool needs to start; secrets and provider keys are never passed on.


def _fail(message: str) -> DomainError:
    return DomainError(ErrorCode.INVALID_INPUT, message)


def _digest(root: Path) -> str:
    from agent_dev.adapters.skill_store import tree_digest

    folder = root / HOOKS_DIR
    if folder.is_symlink():
        raise _fail(".ai/hooks 폴더는 심볼릭 링크일 수 없습니다.")
    try:
        return tree_digest(folder)
    except DomainError:
        raise _fail(".ai/hooks 안에 심볼릭 링크가 있습니다.") from None


def read_hooks(root) -> dict:
    """{"events": {event: [hook]}, "digest": str}; empty when the project has no hooks."""
    root = Path(root)
    path = root / HOOKS_FILE
    if not path.is_file():
        return {"events": {}, "digest": ""}
    if path.is_symlink():
        raise _fail("hooks.json은 심볼릭 링크일 수 없습니다.")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise _fail("hooks.json이 64KiB를 넘습니다.")
    try:
        text = raw.decode("utf-8")
        value = json.loads(text)
    except (UnicodeDecodeError, ValueError):
        raise _fail("hooks.json은 UTF-8 JSON이어야 합니다.") from None
    if redact(text) != text:
        raise _fail("hooks.json에 비밀값으로 보이는 내용이 있습니다. 환경변수 이름만 쓰세요.")
    if not isinstance(value, dict):
        raise _fail("hooks.json은 {\"이벤트\": [훅, ...]} 형태여야 합니다.")
    events = {}
    for event, hooks in value.items():
        if event not in EVENTS:
            raise _fail(f"알 수 없는 훅 시점입니다: {event} (사용 가능: {', '.join(EVENTS)})")
        if not isinstance(hooks, list) or len(hooks) > MAX_HOOKS_PER_EVENT:
            raise _fail(f"{event}에는 훅 목록(최대 10개)을 적어주세요.")
        rows = []
        for index, hook in enumerate(hooks):
            command = hook.get("run") if isinstance(hook, dict) else None
            if (
                not isinstance(command, list)
                or not command
                or len(command) > 64
                or not all(isinstance(a, str) and a and len(a) <= 1000 for a in command)
            ):
                raise _fail(f"{event}[{index}]의 run은 명령과 인수의 배열이어야 합니다 (셸 문자열 금지).")
            timeout = hook.get("timeout", DEFAULT_TIMEOUT)
            if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
                raise _fail(f"{event}[{index}]의 timeout은 1~600초입니다.")
            name = hook.get("name") or " ".join(command)[:80]
            rows.append({"name": str(name)[:80], "run": command, "timeout": timeout})
        if rows:
            events[event] = rows
    return {"events": events, "digest": _digest(root) if events else ""}


def approved_digest(config_root) -> str:
    """The approved hook digest, kept in the management folder (workspace_trust).

    An approval file inside the project (APPROVAL_FILE, written by older versions) is
    ignored: a repository could ship one. An untrusted workspace has no approval.
    """
    from agent_dev.adapters import workspace_trust

    if not workspace_trust.is_trusted(config_root):
        return ""
    return workspace_trust.hook_approval(config_root)


def approve(config_root, project_root, digest_value: str, actor: str = "local-user") -> dict:
    """Approve exactly the hooks the user reviewed; a newer folder must be reviewed again."""
    if Path(config_root).resolve() == Path(project_root).resolve():
        # Approval is personal and local; it must never land in the repository itself.
        raise _fail("DevRoster 작업 공간으로 설정한 프로젝트에서만 훅을 승인할 수 있습니다.")
    current = read_hooks(project_root)
    if not current["events"]:
        raise _fail("승인할 훅이 없습니다.")
    if digest_value != current["digest"]:
        raise DomainError(ErrorCode.STALE_VERSION, "확인한 뒤 훅이 바뀌었습니다. 다시 확인해주세요.")
    from agent_dev.adapters import workspace_trust

    workspace_trust.require(config_root, "훅 승인")
    workspace_trust.approve_hooks(config_root, digest_value, actor)
    return {"digest": digest_value}


def snapshot_value(project_root, config_root) -> dict | None:
    """Hooks frozen into an approved task; refuses unapproved or changed hooks."""
    hooks = read_hooks(project_root)
    if not hooks["events"]:
        return None
    if approved_digest(config_root) != hooks["digest"]:
        raise DomainError(
            ErrorCode.APPROVAL_REQUIRED,
            "프로젝트 훅(.ai/hooks)이 아직 승인되지 않았거나 승인 후 바뀌었습니다. "
            "프로젝트 설정 > 개발 역할 > 프로젝트 훅에서 내용을 확인하고 승인해주세요.",
        )
    return hooks


def _command(project_root: Path, command: list[str]) -> list[str]:
    first = command[0].replace("\\", "/")
    if first.startswith(("./.ai/hooks/", ".ai/hooks/")):
        # Scripts run from the approved project folder, not from a worktree copy.
        script = (project_root / first.removeprefix("./")).resolve()
        if not script.is_relative_to((project_root / HOOKS_DIR).resolve()):
            raise _fail("훅 스크립트는 .ai/hooks 안에 있어야 합니다.")
        return [str(script), *command[1:]]
    return list(command)


def run(event: str, bundle: dict | None, project_root, cwd, context: dict, secrets=()) -> list[dict]:
    """Run the event's hooks in order. Blocking events stop at the first failure."""
    if not bundle or event not in bundle.get("events", {}):
        return []
    from agent_dev.adapters.bounded_process import run_bounded

    project_root = Path(project_root)
    try:
        unchanged = _digest(project_root) == bundle["digest"]
    except DomainError:
        unchanged = False
    if not unchanged:
        return [{
            "event": event,
            "name": "승인 확인",
            "ok": False,
            "exit_code": None,
            "seconds": 0,
            "output": "승인한 뒤 .ai/hooks가 바뀌어 훅을 실행하지 않았습니다. 다시 승인한 뒤 새 작업에서 사용됩니다.",
        }]
    from agent_dev.adapters.process_launcher import environment

    env = environment(
        "hook",
        extra={
            "DEVROSTER_EVENT": event,
            "DEVROSTER_PROJECT_ROOT": str(project_root),
            **{f"DEVROSTER_{k.upper()}": str(v) for k, v in context.items() if v is not None},
        },
    )
    results = []
    for hook in bundle["events"][event]:
        started = time.monotonic()
        try:
            process = run_bounded(
                _command(project_root, hook["run"]),
                cwd=Path(cwd),
                env=env,
                timeout=hook["timeout"],
                limit=1_000_000,
                tail=OUTPUT_TAIL,
            )
            code = 125 if process.overflow else process.returncode
            output = process.output.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            code, output = 124, f"제한 시간({hook['timeout']}초)을 넘겨 중단했습니다."
        except DomainError as exc:
            code, output = 126, str(exc)
        except OSError:
            code, output = 127, "명령을 실행할 수 없습니다. 실행 파일 이름과 경로를 확인해주세요."
        result = {
            "event": event,
            "name": hook["name"],
            "ok": code == 0,
            "exit_code": code,
            "seconds": round(time.monotonic() - started, 2),
            "output": redact(output[-OUTPUT_TAIL:], tuple(secrets)),
        }
        results.append(result)
        if not result["ok"] and EVENTS[event] == "block":
            break
    return results


def failure_text(results: list[dict]) -> str:
    failed = next((r for r in results if not r["ok"]), None)
    if not failed:
        return ""
    return f"{failed['event']} 훅 '{failed['name']}' 실패 (종료 코드 {failed['exit_code']}): {failed['output'][-1500:]}"


def overview(project_root, config_root) -> dict:
    try:
        hooks = read_hooks(project_root)
        error = ""
    except DomainError as exc:
        hooks, error = {"events": {}, "digest": ""}, str(exc)
    return {
        "events": hooks["events"],
        "digest": hooks["digest"],
        "approved": bool(hooks["digest"]) and approved_digest(config_root) == hooks["digest"],
        "modes": EVENTS,
        "error": error,
    }


# Record-only moments follow the task's own events, so every code path that reaches the
# moment triggers them. They run on a worker thread after the event, never inside its
# transaction, and their failure is only recorded.
OBSERVED = {
    "PhaseStarted": "phase_start",
    "PhaseFinished": "phase_end",
    "VerificationFinished": "after_verify",
    "PublishRecorded": "after_publish",
    "ExecutionBlocked": "on_problem",
    "FindingDetected": "on_problem",
    "UsageThresholdReached": "on_problem",
}


class HookObserver:
    def __init__(self, runner):
        import queue
        import threading

        self.runner = runner  # (task_id, event, context) -> results; records HookRun itself.
        self.queue: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.worker = None
        self.errors: list[str] = []

    def notify(self, task_id, kind, payload):
        event = OBSERVED.get(kind)
        if not event:
            return
        context = {"kind": kind}
        for key in ("phase_id", "state", "passed", "code", "reason"):
            if key in payload:
                context["phase" if key == "phase_id" else "result" if key in {"state", "passed"} else key] = payload[key]
        self.queue.put((task_id, event, context))
        import threading

        with self.lock:
            if self.worker is None or not self.worker.is_alive():
                # Not a daemon: a short CLI run waits for its hooks before exiting.
                self.worker = threading.Thread(target=self._drain, name="devroster-hooks")
                self.worker.start()

    def _drain(self):
        import queue

        while True:
            try:
                task_id, event, context = self.queue.get(timeout=0.5)
            except queue.Empty:
                return
            try:
                self.runner(task_id, event, context)
            except Exception as exc:  # noqa: BLE001 -- a record-only hook never breaks the task.
                self.errors.append(f"{event}: {type(exc).__name__}")


LOG_FILE = "ai-hooks-log.jsonl"
LOG_KEEP = 200


def _config(root) -> Path:
    from agent_dev.adapters.workspace_locator import settings_root

    return settings_root(root)


def log_results(config_root, event: str, results: list[dict], context: dict) -> None:
    """Keep the last runs of hooks that have no task to record them on (Studio)."""
    path = Path(config_root) / LOG_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    lines.append(json.dumps({"at": time.time(), "event": event, "context": context, "results": results}, ensure_ascii=False))
    path.write_text("\n".join(lines[-LOG_KEEP:]) + "\n", encoding="utf-8")


def recent(config_root, limit: int = 20) -> list[dict]:
    path = Path(config_root) / LOG_FILE
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(rows))


def studio_event(root, context: dict):
    """studio_confirmed: run approved hooks in the background after a Studio confirmation."""
    import threading

    from agent_dev.adapters.workflow_runtime import project_root

    try:
        config = _config(root)
        source = project_root(config)
        hooks = read_hooks(source)
    except (DomainError, OSError, ValueError):
        return None
    if "studio_confirmed" not in hooks["events"] or approved_digest(config) != hooks["digest"]:
        return None
    bundle = dict(hooks, root=str(source))

    def work():
        results = run("studio_confirmed", bundle, source, source, context)
        log_results(config, "studio_confirmed", results, context)

    thread = threading.Thread(target=work, name="devroster-studio-hooks")
    thread.start()
    return thread
