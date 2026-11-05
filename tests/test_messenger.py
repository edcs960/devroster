"""Slack messenger: command parsing, settings, the listener and the settings page."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from agent_dev.adapters import messenger
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.errors import DomainError, ErrorCode

USER, OTHER, BOT = "U0ALLOWED1", "U0STRANGER", "B0PICKLE01"
CHANNEL = "C0DEVROST1"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("도움말", {"kind": "help"}),
        ("<@U0BOT0001> help", {"kind": "help"}),
        ("상태", {"kind": "status", "task_id": ""}),
        ("status TASK-001", {"kind": "status", "task_id": "TASK-001"}),
        ("실행 TASK-001", {"kind": "run", "action": "run", "task_id": "TASK-001"}),
        ("resume TASK-001", {"kind": "run", "action": "resume", "task_id": "TASK-001"}),
        ("승인 설계 DESIGN-1 2", {"kind": "approve", "target": "design", "id": "DESIGN-1", "version": 2}),
        ("approve plan PLAN-1", {"kind": "approve", "target": "plan", "id": "PLAN-1"}),
        (
            "승인 단계 TASK-001 approve-analysis",
            {"kind": "approve", "target": "step", "task_id": "TASK-001", "node_id": "approve-analysis"},
        ),
        ("요청 회원 탈퇴\n탈퇴 시 데이터 삭제", {"kind": "request", "title": "회원 탈퇴", "brief": "탈퇴 시 데이터 삭제"}),
        ("요청 로그인 개선", {"kind": "request", "title": "로그인 개선", "brief": "로그인 개선"}),
        ("안녕하세요", {"kind": "unknown"}),
        ("", None),
    ],
)
def test_parse(text, expected):
    assert messenger.parse(text) == expected


def test_parse_rejects_malformed_arguments_and_honors_the_prefix():
    assert messenger.parse("실행")["kind"] == "invalid"
    assert messenger.parse("실행 TASK-001; rm -rf /")["kind"] == "invalid"
    assert messenger.parse("승인 설계 DESIGN-1")["kind"] == "invalid"
    assert messenger.parse("요청")["kind"] == "invalid"
    assert messenger.parse("상태", prefix="devroster") is None
    assert messenger.parse("devroster 상태", prefix="devroster") == {"kind": "status", "task_id": ""}


def test_settings_validation(tmp_path):
    root = tmp_path
    assert messenger.read_settings(root)["commands"]["approve"] is False
    with pytest.raises(DomainError):
        messenger.save_settings(root, {"enabled": True, "channel_id": CHANNEL})  # no allowlist
    with pytest.raises(DomainError):
        messenger.save_settings(root, {"channel_id": "general"})
    with pytest.raises(DomainError):
        messenger.save_settings(root, {"poll_seconds": 5})
    saved = messenger.save_settings(
        root, {"enabled": True, "channel_id": CHANNEL, "allowed_bot_ids": [BOT, BOT]}
    )
    assert saved["allowed_bot_ids"] == [BOT]
    assert messenger.read_settings(root)["enabled"] is True


def test_token_is_stored_privately_and_never_listed(tmp_path):
    with pytest.raises(DomainError):
        messenger.save_token("not-a-token")
    messenger.save_token("xoxb-fixture-secret")
    assert messenger.token_saved()
    assert "xoxb-fixture-secret" not in json.dumps(messenger.status(tmp_path))
    messenger.delete_token()
    assert not messenger.token_saved()


class FakeSlack:
    def __init__(self):
        self.messages, self.posts = [], []

    def history(self, channel, oldest, limit=50):
        assert channel == CHANNEL
        return [m for m in self.messages if float(m["ts"]) > float(oldest)]

    def post(self, channel, text, thread_ts=None):
        self.posts.append((thread_ts, text))
        return {"ok": True}


class FakeConsole:
    def __init__(self):
        self.started, self.rows = [], {}

    def start(self, payload):
        self.started.append(payload)
        self.rows[payload["request_id"]] = {"id": payload["request_id"], "state": "running"}

    def history(self, limit=50):
        return list(self.rows.values())


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(json.dumps({"orchestrator_v2": {}}))
    WorkflowRuntime(root).store.register_project("P", "Project", {})
    messenger.save_settings(
        root,
        {
            "enabled": True,
            "channel_id": CHANNEL,
            "allowed_user_ids": [USER],
            "allowed_bot_ids": [BOT],
        },
    )
    return root


def listener(root, slack, console, clock):
    return messenger.MessengerListener(
        root, client_factory=lambda: slack, console=console, clock=lambda: clock[0]
    )


def test_listener_handles_only_new_allowlisted_messages_once(workspace):
    slack, console, clock = FakeSlack(), FakeConsole(), [1000.0]
    app = listener(workspace, slack, console, clock)
    assert "started" in app.poll_once()  # first start: cursor only, no history replay
    slack.messages = [
        {"ts": "999.0", "user": USER, "text": "도움말"},  # before the cursor
        {"ts": "1001.0", "user": USER, "text": "도움말"},
        {"ts": "1002.0", "user": OTHER, "text": "실행 TASK-001"},  # not allowlisted
        {"ts": "1003.0", "bot_id": BOT, "subtype": "bot_message", "text": "상태"},
        {"ts": "1004.0", "user": USER, "text": "실행 TASK-001", "thread_ts": "1001.0"},  # reply
        {"ts": "1005.0", "user": USER, "subtype": "message_changed", "text": "실행 TASK-001"},
        {"ts": "1006.0", "user": USER, "text": "승인 계획 PLAN-1"},  # approve is off
        {"ts": "1007.0", "user": USER, "text": "점심 뭐 먹지"},
    ]
    assert app.poll_once() == {"handled": 4}
    replies = dict(slack.posts)
    assert replies["1001.0"].startswith("*DevRoster 명령*")
    assert replies["1003.0"] == "작업이 없습니다."
    assert "꺼져 있습니다" in replies["1006.0"]
    assert "도움말" in replies["1007.0"]
    assert not console.started
    assert app.poll_once() == {"handled": 0}  # each message once
    assert messenger.status(workspace)["handled"] == 4


def test_run_is_queued_and_its_result_posted_to_the_thread(workspace):
    slack, console, clock = FakeSlack(), FakeConsole(), [1000.0]
    app = listener(workspace, slack, console, clock)
    app.poll_once()
    slack.messages = [{"ts": "1001.5", "user": USER, "text": "실행 TASK-001"}]
    app.poll_once()
    (payload,) = console.started
    assert payload["operation"] == "task.run" and payload["fields"] == {"task_id": "TASK-001"}
    assert payload["request_id"] == "slack-C0DEVROST1-1001-5"
    assert "접수" in slack.posts[-1][1]
    console.rows[payload["request_id"]] = {
        "id": payload["request_id"],
        "state": "completed",
        "result": {"state": "verifying"},
    }
    app.poll_once()
    assert slack.posts[-1] == ("1001.5", "🔍 *TASK-001* 실행 끝 — 작업 상태 *결과 검증이 필요해요*")
    assert messenger.status(workspace)["pending"] == 0


def test_only_one_listener_runs(workspace):
    clock = [1000.0]
    first = listener(workspace, FakeSlack(), FakeConsole(), clock)
    first.poll_once()
    second = listener(workspace, FakeSlack(), FakeConsole(), clock)
    with pytest.raises(DomainError) as error:
        second.poll_once()
    assert error.value.code == ErrorCode.POLICY_VIOLATION
    clock[0] += 1000  # the first listener stopped beating
    assert "handled" in second.poll_once()


def test_disabled_settings_do_not_poll(workspace):
    settings = messenger.read_settings(workspace)
    messenger.save_settings(workspace, dict(settings, enabled=False))
    app = listener(workspace, None, FakeConsole(), [1000.0])
    assert app.poll_once() == {"skipped": "disabled"}


def test_settings_page_saves_without_echoing_the_token(workspace):
    from agent_dev import app

    server = app.make_server(workspace, 0)
    try:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/messengers", timeout=5) as page:
            assert "외부 메신저 연결" in page.read().decode()
        with urllib.request.urlopen(base + "/api/messengers/bootstrap", timeout=5) as response:
            boot = json.loads(response.read())
        assert boot["settings"]["channel_id"] == CHANNEL and boot["token_saved"] is False

        def post(path, payload, token=boot["token"]):
            request = urllib.request.Request(
                base + path,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "X-Local-Token": token},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read())

        saved = post("/api/messengers/token", {"value": "xoxb-page-secret"})
        assert saved["token_saved"] is True and "xoxb-page-secret" not in json.dumps(saved)
        with pytest.raises(urllib.error.HTTPError) as error:
            post("/api/messengers/save", {"settings": {"channel_id": "general"}})
        assert error.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as error:
            post("/api/messengers/token", {"value": "xoxb-x"}, token="wrong")
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()


class FakeStudioWeb:
    def __init__(self):
        self.created, self.started, self.job_state = [], [], "running"
        outer = self

        class Store:
            def detail(self, project_id):
                return {"jobs": [{"state": outer.job_state, "message": "timeout"}]}

        class Studio:
            store = Store()

            def create(self, title, brief):
                outer.created.append((title, brief))
                return {"project": {"id": "STUDIO-1", "version": 1}}

        class Jobs:
            def start(self, project_id, version, kind):
                outer.started.append((project_id, version, kind))

        self.studio, self.jobs = Studio(), Jobs()


def test_request_creates_a_studio_draft_and_reports_the_spec(workspace):
    slack, clock, studio = FakeSlack(), [1000.0], FakeStudioWeb()
    app = messenger.MessengerListener(
        workspace, studio_web=studio, client_factory=lambda: slack, console=FakeConsole(),
        clock=lambda: clock[0],
    )
    app.poll_once()
    slack.messages = [{"ts": "1001.0", "bot_id": BOT, "subtype": "bot_message", "text": "요청 회원 탈퇴\n본문"}]
    app.poll_once()
    assert studio.created == [("회원 탈퇴", "본문")] and studio.started == [("STUDIO-1", 1, "spec")]
    studio.job_state = "done"
    app.poll_once()
    assert slack.posts[-1] == ("1001.0", "✅ *STUDIO-1* 명세 생성 완료. Studio에서 검토해주세요.")


def test_approvals_use_the_cli_rules_and_record_the_slack_actor(workspace):
    settings = messenger.read_settings(workspace)
    settings["commands"]["approve"] = True
    messenger.save_settings(workspace, settings)
    slack, clock = FakeSlack(), [1000.0]
    app = listener(workspace, slack, FakeConsole(), clock)
    app.poll_once()
    slack.messages = [
        {"ts": "1001.0", "user": USER, "text": "승인 계획 PLAN-404"},
        {"ts": "1002.0", "user": USER, "text": "승인 단계 TASK-404 approve-analysis"},
    ]
    app.poll_once()
    replies = dict(slack.posts)
    assert replies["1001.0"].startswith("❌ 계획 *PLAN-404* 승인 실패")
    assert "처리하지 못했습니다" in replies["1002.0"] or "승인 대기 상태가 아닙니다" in replies["1002.0"]


def test_design_approval_from_slack_is_recorded_with_the_slack_actor(workspace):
    settings = messenger.read_settings(workspace)
    settings["commands"]["approve"] = True
    messenger.save_settings(workspace, settings)
    runtime = WorkflowRuntime(workspace)
    runtime.store.design("P", "D", "## Scope\nx\n## Acceptance\ny\n## Constraints\nz")
    slack, clock = FakeSlack(), [1000.0]
    app = listener(workspace, slack, FakeConsole(), clock)
    app.poll_once()
    slack.messages = [{"ts": "1001.0", "user": USER, "text": "승인 설계 D 1"}]
    app.poll_once()
    assert slack.posts[-1] == ("1001.0", "✅ 설계 *D* v1 승인 완료")
    rows = WorkflowRuntime(workspace).store.db.rows("SELECT * FROM approvals")
    assert [row["actor"] for row in rows] == ["slack:" + USER]


def test_state_changes_are_posted_after_a_silent_baseline(workspace):
    slack, console, clock = FakeSlack(), FakeConsole(), [1000.0]
    app = listener(workspace, slack, console, clock)
    tasks = [{"id": "TASK-001", "title": "로그인", "state": "ready"}, {"id": "TASK-002", "title": "목록", "state": "completed"}]
    app._tasks = lambda: tasks
    app.poll_once()  # cursor and baseline: nothing posted
    app.poll_once()
    assert slack.posts == []
    slack.messages = [{"ts": "1001.5", "user": USER, "text": "실행 TASK-001"}]
    tasks[0] = dict(tasks[0], state="running")
    app.poll_once()
    # The run command's thread receives the task's own changes.
    assert slack.posts[-1] == ("1001.5", "⏳ *TASK-001* 로그인\n시작할 수 있어요 → *작업 진행 중*")
    tasks.append({"id": "TASK-003", "title": "신규", "state": "approval_pending"})
    app.poll_once()
    assert slack.posts[-1] == (None, "📝 *TASK-003* 신규\n새 작업 → *승인을 기다려요*")
    assert messenger.status(workspace)["recent"][0]["kind"] == "notify"


def test_an_approval_gate_names_the_slack_command(workspace):
    settings = messenger.read_settings(workspace)
    settings["commands"]["approve"] = True
    messenger.save_settings(workspace, settings)
    slack, clock = FakeSlack(), [1000.0]
    app = listener(workspace, slack, FakeConsole(), clock)
    tasks = [{"id": "TASK-001", "title": "로그인", "state": "running"}]
    app._tasks = lambda: tasks
    app._explain = lambda task_id: {"node_states": {"planner": "passed", "approve-analysis": "awaiting_approval"}}
    app.poll_once()
    app.poll_once()
    tasks[0] = dict(tasks[0], state="blocked")
    app.poll_once()
    assert slack.posts[-1][1] == (
        "🙋 *TASK-001* 로그인 — *승인 대기* (`approve-analysis`)\n"
        "승인: `승인 단계 TASK-001 approve-analysis`"
    )


def test_notifications_can_be_turned_off(workspace):
    settings = messenger.read_settings(workspace)
    messenger.save_settings(workspace, dict(settings, notify=False))
    slack, clock = FakeSlack(), [1000.0]
    app = listener(workspace, slack, FakeConsole(), clock)
    tasks = [{"id": "TASK-001", "title": "로그인", "state": "ready"}]
    app._tasks = lambda: tasks
    app.poll_once()
    app.poll_once()
    tasks[0] = dict(tasks[0], state="running")
    app.poll_once()
    assert slack.posts == []


def test_slack_text_from_tasks_cannot_mention_or_link(workspace):
    slack, clock = FakeSlack(), [1000.0]
    app = listener(workspace, slack, FakeConsole(), clock)
    tasks = [{"id": "TASK-001", "title": "<!channel> & <https://x.test|y>", "state": "ready"}]
    app._tasks = lambda: tasks
    app.poll_once()
    app.poll_once()
    tasks[0] = dict(tasks[0], state="running")
    app.poll_once()
    text = slack.posts[-1][1]
    assert "<!channel>" not in text and "&lt;!channel&gt; &amp; &lt;https://x.test|y&gt;" in text
