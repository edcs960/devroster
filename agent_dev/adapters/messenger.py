"""Slack messenger: settings, command parsing and the listener that runs DevRoster commands.

Design: docs/design/slack-messenger.md. Messages are untrusted
data; only allowlisted authors in one channel reach the existing CLI, agent-console and
Studio paths, so approval, snapshot and verification rules stay those of the web and CLI.
"""

import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path

from agent_dev.adapters.credential_store import CredentialStore
from agent_dev.domain.errors import DomainError, ErrorCode

CONNECTION = "messenger-slack"
TOKEN_KEY = "SLACK_BOT_TOKEN"
COMMANDS = ("request", "status", "run", "approve")
DEFAULTS = {
    "enabled": False,
    "channel_id": "",
    "allowed_user_ids": [],
    "allowed_bot_ids": [],
    "prefix": "",
    "poll_seconds": 15,
    "commands": {"request": True, "status": True, "run": True, "approve": False},
    # Post every task state change to the channel (thread of the Slack command when known).
    "notify": True,
}
NOTIFY_LIMIT = 20
CHANNEL = re.compile(r"[CG][A-Z0-9]{6,20}")
USER = re.compile(r"[UW][A-Z0-9]{6,20}")
BOT = re.compile(r"B[A-Z0-9]{6,20}")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
MENTION = re.compile(r"^(<@[A-Z0-9]+>\s*)+")
RECENT = 20
PROCESSED = 500
HELP = """*DevRoster 명령* (메시지 첫 줄로 판단합니다)
• `요청 <제목>` — 다음 줄부터 기획 내용. Studio 기획서를 만들고 명세 생성
• `상태` · `상태 <작업 ID>` — 작업 목록 · 한 작업의 상태
• `실행` · `재개` · `검증 <작업 ID>` — 승인된 작업 진행
• `승인 설계 <ID> <버전>` · `승인 계획 <ID>` · `승인 단계 <작업 ID> <단계 ID>`
• `도움말` — 이 목록"""
ICONS = {
    "planned": "🗂️",
    "approval_pending": "📝",
    "ready": "🟢",
    "running": "⏳",
    "blocked": "⚠️",
    "waiting_for_capacity": "⏸️",
    "verifying": "🔍",
    "pr_ready": "👀",
    "user_review": "👀",
    "feedback_required": "✏️",
    "completed": "✅",
    "cancelled": "⛔",
}
KIND_NAMES = {"request": "요청", "status": "상태", "run": "실행", "approve": "승인"}
ACTION_NAMES = {"run": "실행", "resume": "재개", "verify": "검증"}


def esc(value):
    """Slack mrkdwn text: `<`, `>` and `&` would otherwise form mentions or links."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def state_label(value):
    from agent_dev.ui.presentation import STATES

    return STATES.get(value, (value,))[0] if value else "새 작업"


def task_title(task_id, title):
    return f"*{esc(task_id)}* {esc(title)}".rstrip()


def invalid(message):
    raise DomainError(ErrorCode.INVALID_INPUT, message)


# -- locations and settings ------------------------------------------------------------


def config_folder(root):
    from agent_dev.adapters.workspace import configuration_root, engine_configuration

    return engine_configuration(configuration_root(Path(root)))


def settings_path(root):
    return config_folder(root) / "config" / "messenger.json"


def state_path(root):
    return config_folder(root) / "data" / "messenger-state.json"


def _read_json(path, default):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return copy.deepcopy(default)
    return value if isinstance(value, dict) else copy.deepcopy(default)


def _write_json(path, value):
    from agent_dev.adapters.project_setup import atomic_json

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    atomic_json(Path(path), value)


def read_settings(root):
    stored = _read_json(settings_path(root), {}).get("slack", {})
    value = copy.deepcopy(DEFAULTS)
    value.update({k: v for k, v in stored.items() if k in DEFAULTS and k != "commands"})
    value["commands"].update(
        {k: v for k, v in stored.get("commands", {}).items() if k in COMMANDS}
    )
    return value


def normalize_settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        invalid("메신저 설정 항목을 확인해주세요.")
    result = copy.deepcopy(DEFAULTS)
    result.update({k: v for k, v in value.items() if k != "commands"})
    result["commands"].update(value.get("commands", {}))
    if type(result["enabled"]) is not bool or type(result["notify"]) is not bool:
        invalid("사용 여부를 확인해주세요.")
    channel = result["channel_id"]
    if not isinstance(channel, str) or (channel and not CHANNEL.fullmatch(channel)):
        invalid("채널 ID는 C 또는 G로 시작하는 슬랙 채널 ID입니다.")
    for key, pattern, label in (
        ("allowed_user_ids", USER, "사용자 ID(U…)"),
        ("allowed_bot_ids", BOT, "봇 ID(B…)"),
    ):
        ids = result[key]
        if not isinstance(ids, list) or len(ids) > 50 or not all(
            isinstance(i, str) and pattern.fullmatch(i) for i in ids
        ):
            invalid(f"허용 {label} 형식을 확인해주세요.")
        result[key] = sorted(set(ids))
    prefix = result["prefix"]
    if not isinstance(prefix, str) or len(prefix) > 20 or any(c in prefix for c in "\r\n\t"):
        invalid("머리말은 줄바꿈 없이 20자 이하로 입력해주세요.")
    if type(result["poll_seconds"]) is not int or not 10 <= result["poll_seconds"] <= 300:
        invalid("조회 주기는 10~300초입니다.")
    commands = result["commands"]
    if set(commands) != set(COMMANDS) or any(type(v) is not bool for v in commands.values()):
        invalid("명령별 허용 값을 확인해주세요.")
    if result["enabled"] and not (
        channel and (result["allowed_user_ids"] or result["allowed_bot_ids"])
    ):
        invalid("사용하려면 채널 ID와 허용 사용자 또는 봇 ID가 필요합니다.")
    return result


def save_settings(root, value):
    result = normalize_settings(value)
    _write_json(settings_path(root), {"slack": result})
    return result


def token_saved():
    return bool(CredentialStore.shared_entry(CONNECTION).get(TOKEN_KEY))


def save_token(value):
    if not isinstance(value, str) or not value.strip().startswith(("xoxb-", "xoxp-")):
        invalid("슬랙 봇 토큰은 xoxb-로 시작합니다.")
    CredentialStore.save_shared(CONNECTION, TOKEN_KEY, value.strip())


def delete_token():
    CredentialStore.remove_shared_key(CONNECTION, TOKEN_KEY)


def client():
    from agent_dev.adapters.slack_client import SlackClient

    return SlackClient(CredentialStore.shared_entry(CONNECTION).get(TOKEN_KEY, ""))


def check(root, slack=None):
    """Token identity and channel access, without reading messages."""
    settings = read_settings(root)
    slack = slack or client()
    identity = slack.identity()
    result = {"team": identity.get("team", ""), "bot_user": identity.get("user", ""), "channel": None}
    if settings["channel_id"]:
        channel = slack.channel(settings["channel_id"])
        result["channel"] = {
            "name": channel.get("name", ""),
            "is_member": bool(channel.get("is_member")),
        }
    return result


# -- command parsing --------------------------------------------------------------------

WORDS = {
    "도움말": "help", "help": "help", "?": "help",
    "요청": "request", "request": "request",
    "상태": "status", "status": "status",
    "실행": "run", "run": "run", "재개": "resume", "resume": "resume",
    "검증": "verify", "verify": "verify",
    "승인": "approve", "approve": "approve",
}
TARGETS = {"설계": "design", "design": "design", "계획": "plan", "plan": "plan", "단계": "step", "step": "step"}


def parse(text, prefix=""):
    """``{"kind": ..., ...}`` for one message, or None when it is not addressed to DevRoster."""
    text = MENTION.sub("", (text or "").strip())
    if prefix:
        if not text.lower().startswith(prefix.lower()):
            return None
        text = text[len(prefix):].strip()
    if not text:
        return None
    first, _, rest = text.partition("\n")
    words = first.split()
    kind = WORDS.get(words[0].lower())
    if kind is None:
        return {"kind": "unknown"}
    args = words[1:]
    ids_ok = all(IDENTIFIER.fullmatch(a) for a in args)
    if kind == "help":
        return {"kind": "help"}
    if kind == "request":
        title = first.split(None, 1)[1].strip() if len(words) > 1 else ""
        if not title:
            return {"kind": "invalid", "reason": "요청 제목이 필요합니다. 예: 요청 회원 탈퇴 기능"}
        return {"kind": "request", "title": title[:120], "brief": (rest.strip() or title)[:20000]}
    if kind == "status":
        if len(args) > 1 or not ids_ok:
            return {"kind": "invalid", "reason": "예: 상태 또는 상태 TASK-001"}
        return {"kind": "status", "task_id": args[0] if args else ""}
    if kind in {"run", "resume", "verify"}:
        if len(args) != 1 or not ids_ok:
            return {"kind": "invalid", "reason": f"예: {words[0]} TASK-001"}
        return {"kind": "run", "action": kind, "task_id": args[0]}
    target = TARGETS.get(args[0].lower()) if args else None
    if not ids_ok and target is None:
        return {"kind": "invalid", "reason": "승인 대상 ID를 확인해주세요."}
    if target == "design" and len(args) == 3 and args[2].isdigit() and IDENTIFIER.fullmatch(args[1]):
        return {"kind": "approve", "target": "design", "id": args[1], "version": int(args[2])}
    if target == "plan" and len(args) == 2 and IDENTIFIER.fullmatch(args[1]):
        return {"kind": "approve", "target": "plan", "id": args[1]}
    if target == "step" and len(args) == 3 and all(IDENTIFIER.fullmatch(a) for a in args[1:]):
        return {"kind": "approve", "target": "step", "task_id": args[1], "node_id": args[2]}
    return {
        "kind": "invalid",
        "reason": "예: 승인 설계 DESIGN-1 1 / 승인 계획 PLAN-1 / 승인 단계 TASK-001 approve-analysis",
    }


# -- listener ---------------------------------------------------------------------------


def _failure(envelope):
    """The error message of a rejected command envelope, or "" when it succeeded."""
    if envelope.get("status") == "rejected" or envelope.get("error"):
        return (envelope.get("error") or {}).get("message") or "처리하지 못했습니다."
    return ""


def _outcome(subject, envelope):
    failure = _failure(envelope)
    return f"❌ {subject} 실패: {esc(failure)}" if failure else f"✅ {subject} 완료"


class MessengerListener:
    def __init__(self, root, *, studio_web=None, client_factory=client, console=None, clock=time.time):
        self.root = Path(root)
        self.studio_web = studio_web
        self.client_factory = client_factory
        self._console = console
        self.clock = clock
        self.owner = uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    # state ----------------------------------------------------------------------------

    def state(self):
        return _read_json(
            state_path(self.root),
            {"cursor": "", "processed": [], "pending": {}, "recent": [], "handled": 0},
        )

    def _save(self, state):
        state["processed"] = state.get("processed", [])[-PROCESSED:]
        state["recent"] = state.get("recent", [])[-RECENT:]
        _write_json(state_path(self.root), state)

    def _claim(self, state, settings):
        holder = state.get("owner")
        beat = state.get("heartbeat", 0)
        if holder and holder != self.owner and self.clock() - beat < 3 * settings["poll_seconds"] + 5:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "다른 리스너가 이미 동작 중입니다(앱 또는 터미널). 하나만 실행해주세요.",
            )
        state.update(owner=self.owner, heartbeat=self.clock())

    # thread ---------------------------------------------------------------------------

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, daemon=True, name="devroster-slack")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=20)
        with self._lock:
            state = self.state()
            if state.get("owner") == self.owner:
                state.update(owner="", heartbeat=0)
                self._save(state)

    def run(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except DomainError as error:
                self._error(str(error))
            except Exception as error:  # noqa: BLE001 -- keep listening; record the type only
                self._error(f"리스너 오류: {type(error).__name__}")
            self._stop.wait(read_settings(self.root)["poll_seconds"])

    def _error(self, message):
        with self._lock:
            state = self.state()
            state.update(last_error=message, last_error_at=self.clock())
            self._save(state)

    # polling --------------------------------------------------------------------------

    def poll_once(self):
        settings = read_settings(self.root)
        if not settings["enabled"]:
            return {"skipped": "disabled"}
        with self._lock:
            state = self.state()
            self._claim(state, settings)
            if not state.get("cursor"):
                # First start: never replay older channel history.
                state["cursor"] = f"{self.clock():.6f}"
                self._save(state)
                return {"started": state["cursor"]}
            slack = self.client_factory()
            handled = 0
            for message in slack.history(settings["channel_id"], state["cursor"]):
                ts = message.get("ts", "")
                state["cursor"] = max(state["cursor"], ts, key=float)
                if ts in state["processed"] or not self._accepted(message, settings):
                    continue
                state["processed"].append(ts)
                command = parse(message.get("text", ""), settings["prefix"])
                if command is None:
                    continue
                author = message.get("user") or message.get("bot_id", "")
                reply, pending = self.handle(command, settings, author, ts)
                slack.post(settings["channel_id"], reply, thread_ts=ts)
                if pending:
                    state["pending"][ts] = pending
                    if pending.get("task_id"):
                        state.setdefault("threads", {})[pending["task_id"]] = ts
                state["recent"].append(
                    {"ts": ts, "author": author, "kind": command["kind"], "reply": reply[:200], "at": self.clock()}
                )
                state["handled"] = state.get("handled", 0) + 1
                handled += 1
            self._follow(slack, settings, state)
            self._notify(slack, settings, state)
            state.update(last_poll=self.clock(), last_error="", heartbeat=self.clock())
            self._save(state)
            return {"handled": handled}

    @staticmethod
    def _accepted(message, settings):
        subtype = message.get("subtype")
        if subtype not in {None, "bot_message"}:
            return False  # edits, joins, deletions
        if message.get("thread_ts") not in {None, message.get("ts")}:
            return False  # thread replies, including the listener's own answers
        if message.get("user") in settings["allowed_user_ids"]:
            return True
        return bool(message.get("bot_id")) and message["bot_id"] in settings["allowed_bot_ids"]

    # commands -------------------------------------------------------------------------

    def handle(self, command, settings, author, ts):
        kind = command["kind"]
        if kind == "help":
            return HELP, None
        if kind == "unknown":
            return "❓ 명령을 알아보지 못했습니다. `도움말`로 목록을 확인해주세요.", None
        if kind == "invalid":
            return "❓ " + command["reason"], None
        if not settings["commands"].get(kind):
            return (
                (
                    f"🚫 `{KIND_NAMES.get(kind, kind)}` 명령은 꺼져 있습니다. "
                    "DevRoster *외부 메신저* 설정에서 켤 수 있습니다."
                ),
                None,
            )
        request_id = "slack-" + re.sub(r"[^A-Za-z0-9]", "-", settings["channel_id"] + "-" + ts)
        actor = "slack:" + author
        try:
            if kind == "status":
                return self._status(command["task_id"]), None
            if kind == "approve":
                return self._approve(command, actor, request_id), None
            if kind == "run":
                return self._run(command, request_id)
            return self._request(command)
        except DomainError as error:
            return f"❌ 처리하지 못했습니다: {esc(error)}", None

    def _cli(self, *arguments, request_id=None):
        from agent_dev.adapters.command_line import execute

        argv = [*arguments, "--root", str(self.root), "--json"]
        if request_id:
            argv += ["--request-id", request_id]
        return execute(argv)[1]

    def _status(self, task_id):
        if task_id:
            envelope = self._cli("task", "explain", task_id)
            failure = _failure(envelope)
            if failure:
                return f"❌ {esc(failure)}"
            result = envelope["result"]
            task = result.get("task") or {}
            state = task.get("state", "")
            lines = [f"{ICONS.get(state, '•')} {task_title(task_id, task.get('title', ''))} — *{state_label(state)}*"]
            lines += [f"• {esc(r.get('message', ''))}" for r in result.get("reasons", [])[:3]]
            lines += [f"→ {esc(action)}" for action in result.get("next_actions", [])[:2]]
            return "\n".join(lines)
        tasks = self._tasks()
        if not tasks:
            return "작업이 없습니다."
        counts = {}
        for task in tasks:
            counts[task.get("state", "")] = counts.get(task.get("state", ""), 0) + 1
        active = [t for t in tasks if t.get("state") not in {"completed", "cancelled"}]
        lines = [
            f"*작업 {len(tasks)}건* · "
            + " · ".join(f"{state_label(k)} {v}" for k, v in sorted(counts.items()))
        ]
        lines += [
            f"{ICONS.get(t.get('state'), '•')} {task_title(t.get('id', ''), t.get('title', ''))}"
            f" — {state_label(t.get('state', ''))}"
            for t in active[:10]
        ]
        if len(active) > 10:
            lines.append(f"… 외 {len(active) - 10}건")
        return "\n".join(lines)

    def _approve(self, command, actor, request_id):
        target = command["target"]
        if target == "design":
            envelope = self._cli(
                "design", "approve", command["id"], str(command["version"]),
                "--actor", actor, request_id=request_id,
            )
            return _outcome(f"설계 *{esc(command['id'])}* v{command['version']} 승인", envelope)
        if target == "plan":
            envelope = self._cli("plan", "approve", command["id"], "--actor", actor, request_id=request_id)
            return _outcome(f"계획 *{esc(command['id'])}* 승인", envelope)
        from agent_dev.adapters.workflow_definitions import WorkflowDefinitions

        nodes = WorkflowDefinitions(self.root).execution_state(command["task_id"])["nodes"]
        node = nodes.get(command["node_id"])
        if not node or node["state"] != "awaiting_approval":
            return (
                f"ℹ️ *{esc(command['task_id'])}*의 `{esc(command['node_id'])}` 단계는 "
                "승인 대기 상태가 아닙니다."
            )
        envelope = self._cli(
            "workflow", "approve", command["task_id"], command["node_id"],
            "--actor", actor, "--expected-version", str(node["version"]),
        )
        if _failure(envelope):
            return _outcome(f"*{esc(command['task_id'])}* `{esc(command['node_id'])}` 승인", envelope)
        return (
            f"✅ *{esc(command['task_id'])}* `{esc(command['node_id'])}` 승인 완료\n"
            f"이어서 진행: `재개 {esc(command['task_id'])}`"
        )

    @property
    def console(self):
        if self._console is None:
            from agent_dev.adapters.agent_console import AgentConsole

            self._console = AgentConsole(self.root)
        return self._console

    def _run(self, command, request_id):
        from agent_dev.domain.content import digest

        payload = {"operation": "task." + command["action"], "fields": {"task_id": command["task_id"]}, "options": {}}
        self.console.start(dict(payload, request_id=request_id, preview_digest=digest(payload)))
        return (
            (
                f"⏳ *{esc(command['task_id'])}* {ACTION_NAMES[command['action']]} 접수\n"
                "끝나면 이 스레드에 결과를 남깁니다."
            ),
            {
                "type": "console",
                "id": request_id,
                "task_id": command["task_id"],
                "action": command["action"],
            },
        )

    def _studio(self):
        if self.studio_web is None:
            from agent_dev.ui.studio_server import StudioWeb

            self.studio_web = StudioWeb(config_folder(self.root))
        return self.studio_web

    def _request(self, command):
        web = self._studio()
        detail = web.studio.create(command["title"], command["brief"])
        project = detail["project"]
        web.jobs.start(project["id"], project["version"], "spec")
        return (
            (
                f"📝 Studio 프로젝트 *{esc(project['id'])}* 를 만들고 명세 생성을 시작했습니다.\n"
                "끝나면 알려드립니다. 명세 확인 · 화면 승인 · 인계는 Studio 화면에서 진행합니다."
            ),
            {"type": "studio", "project_id": project["id"]},
        )

    def _tasks(self):
        return self._cli("task", "list").get("result", {}).get("tasks", [])

    def _explain(self, task_id):
        return self._cli("task", "explain", task_id).get("result") or {}

    def _notify(self, slack, settings, state):
        """Post task state changes since the last poll; the first poll only records."""
        if not settings["notify"]:
            state.pop("task_states", None)
            return
        tasks = [t for t in self._tasks() if t.get("id")]
        known = state.get("task_states")
        state["task_states"] = {t["id"]: t.get("state", "") for t in tasks}
        if known is None:
            return
        threads = state.setdefault("threads", {})
        changed = [t for t in tasks if known.get(t["id"]) != t.get("state", "")][:NOTIFY_LIMIT]
        for task in changed:
            task_id, new = task["id"], task.get("state", "")
            text = self._transition(task, known.get(task_id), new, settings)
            slack.post(settings["channel_id"], text, thread_ts=threads.get(task_id))
            if new in {"completed", "cancelled"}:
                threads.pop(task_id, None)
            state["recent"].append(
                {"ts": "", "author": "DevRoster", "kind": "notify", "reply": text[:200], "at": self.clock()}
            )

    def _transition(self, task, old, new, settings):
        task_id = task["id"]
        head = task_title(task_id, task.get("title", ""))
        text = f"{ICONS.get(new, '🔄')} {head}\n{state_label(old)} → *{state_label(new)}*"
        if new != "blocked":
            return text
        result = self._explain(task_id)
        waiting = [n for n, s in (result.get("node_states") or {}).items() if s == "awaiting_approval"]
        if waiting:
            node = esc(waiting[0])
            hint = (
                f"승인: `승인 단계 {esc(task_id)} {node}`"
                if settings["commands"]["approve"]
                else "DevRoster *개발 흐름* 화면에서 승인해주세요."
            )
            return f"🙋 {head} — *승인 대기* (`{node}`)\n{hint}"
        reasons = result.get("reasons") or []
        return text + (f"\n• {esc(reasons[0].get('message', ''))}" if reasons else "")

    def _follow(self, slack, settings, state):
        """Post the outcome of background work started from earlier messages."""
        if not state.get("pending"):
            return
        history = {row.get("id"): row for row in self.console.history(100)} if any(
            p["type"] == "console" for p in state["pending"].values()
        ) else {}
        for ts, pending in list(state["pending"].items()):
            text = None
            if pending["type"] == "console":
                row = history.get(pending["id"])
                if row and row.get("state") not in {"queued", "running"}:
                    result = row.get("result") or {}
                    task_state = str(result.get("state") or "") if isinstance(result, dict) else ""
                    name = ACTION_NAMES.get(pending.get("action", "run"), "실행")
                    subject = f"*{esc(pending['task_id'])}* {name}"
                    if row.get("state") == "completed":
                        text = f"{ICONS.get(task_state, '✅')} {subject} 끝" + (
                            f" — 작업 상태 *{state_label(task_state)}*" if task_state else ""
                        )
                    else:
                        text = f"❌ {subject} 실패: {esc(row.get('error') or row.get('state'))}"
            else:
                jobs = self._studio().studio.store.detail(pending["project_id"])["jobs"]
                job = jobs[0] if jobs else None
                if job and job["state"] in {"done", "failed"}:
                    project = f"*{esc(pending['project_id'])}*"
                    text = (
                        f"✅ {project} 명세 생성 완료. Studio에서 검토해주세요."
                        if job["state"] == "done"
                        else f"❌ {project} 명세 생성 실패: {esc(job.get('message', ''))}"
                    )
            if text:
                slack.post(settings["channel_id"], text, thread_ts=ts)
                del state["pending"][ts]


def status(root, listener=None):
    """What the settings page and `devroster messenger status` show (no secrets)."""
    settings = read_settings(root)
    state = _read_json(state_path(root), {})
    return {
        "settings": settings,
        "token_saved": token_saved(),
        "running": bool(listener and listener.running),
        "owner_active": bool(
            state.get("owner") and time.time() - state.get("heartbeat", 0) < 3 * settings["poll_seconds"] + 5
        ),
        "last_poll": state.get("last_poll"),
        "last_error": state.get("last_error", ""),
        "handled": state.get("handled", 0),
        "pending": len(state.get("pending", {})),
        "recent": list(reversed(state.get("recent", []))),
    }
