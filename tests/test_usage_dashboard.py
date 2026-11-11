"""Project-wide usage, cost and progress dashboard (/usage)."""

import http.client
import json
import sys
import threading
import time

import pytest

from agent_dev.adapters import execution_view, usage_dashboard
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.usage_policy import normalize_policy
from agent_dev.domain.usage_report import report
from agent_dev.ui.studio_server import make_server as studio_server
from tests.test_execution_view import (
    POLICY,
    Runner,
    runtime,  # noqa: F401 - fixture
)

PRICED = normalize_policy({"currency": "USD", "price_book": POLICY["price_book"]})


def usage_event(attempt, tokens, *, model="gpt-x", phase="developer", ai_call=True, created=1000.0, task="T1"):
    payload = {
        "attempt_id": attempt,
        "phase_id": phase,
        "runner_seconds": 2.5,
        "provider": "codex_cli",
        "usage": {"input_tokens": tokens, "output_tokens": tokens // 10} if tokens is not None else {},
        "selection": {"model": model},
        "ai_call": ai_call,
    }
    return {"task_id": task, "payload": payload, "created_at": created}


def tasks(*states):
    return [{"id": f"T{i}", "title": f"작업 {i}", "state": s, "updated_at": 100.0 + i} for i, s in enumerate(states, 1)]


def test_report_counts_real_calls_only_and_prices_them():
    calls = [
        usage_event("a1", 1_000_000, phase="developer"),
        # A session member shares a1's call: not a second call, not an unpriced call.
        usage_event("a2", None, phase="tester", ai_call=False),
        usage_event("a1", 1_000_000),  # The same attempt recorded twice counts once.
        usage_event("a3", 1_000_000, model="other-model", phase="reviewer"),
    ]
    data = report(tasks("running"), calls, {"T1": PRICED})
    totals = data["totals"]
    assert totals["calls"] == 2 and totals["input_tokens"] == 2_000_000 and totals["output_tokens"] == 200_000
    assert totals["total_tokens"] == 2_200_000 and totals["runner_seconds"] == 5.0
    # gpt-x: 1M input at $1 + 0.1M output at $1; other-model has no price.
    assert totals["cost"] == {"USD": pytest.approx(1.1)}
    assert data["cost"]["known_calls"] == 1 and data["cost"]["unknown_calls"] == 1
    assert data["cost"]["reasons"] == {"price_missing": 1}
    assert [r["model"] for r in data["by_model"]] == ["gpt-x", "other-model"]
    assert [r["role"] for r in data["by_role"]] == ["developer", "reviewer"]
    assert data["by_task"][0]["id"] == "T1" and data["by_task"][0]["calls"] == 2


def test_report_without_price_book_never_shows_zero_cost():
    data = report(tasks("running"), [usage_event("a1", 500)], {"T1": None})
    assert data["totals"]["cost"] == {} and data["cost"]["unpriced_tasks"] == 1
    assert data["cost"]["reasons"] == {"price_book_missing": 1}


def test_report_period_and_daily_series():
    calls = [
        usage_event("old", 100, created=50.0),
        usage_event("d1", 100, created=86400.0 * 2 + 10),
        usage_event("d2", 300, created=86400.0 * 3 + 10),
    ]
    data = report(tasks("running"), calls, {"T1": PRICED}, since=86400.0 * 2, day=lambda ts: f"D{int(ts // 86400)}", days=["D2", "D3", "D4"])
    assert data["totals"]["calls"] == 2
    assert [(d["day"], d["calls"], d["total_tokens"]) for d in data["daily"]] == [
        ("D2", 1, 110), ("D3", 1, 330), ("D4", 0, 0)
    ]


def test_progress_groups_rate_and_attention_order():
    progress = report(
        tasks("completed", "blocked", "running", "approval_pending", "cancelled", "planned", "completed"), [], {}
    )["progress"]
    assert progress["groups"] == {
        "in_progress": 1, "needs_user": 1, "blocked": 1, "planned": 1, "completed": 2, "cancelled": 1
    }
    assert progress["completion_rate"] == pytest.approx(2 / 6, abs=1e-4)
    assert [t["state"] for t in progress["open"]] == ["blocked", "approval_pending", "running", "planned"]
    assert progress["open_total"] == 4
    assert report([], [], {})["progress"]["completion_rate"] is None


def test_summary_reads_a_real_run(runtime):  # noqa: F811
    runtime.execute("T", "run-once", runner=Runner({"input_tokens": 600_000, "cached_input_tokens": 100_000, "output_tokens": 50_000}))
    data = usage_dashboard.summary(runtime.root, 7)
    assert data["ready"] is True and data["days"] == 7 and len(data["daily"]) == 7
    assert data["totals"]["calls"] == 1 and data["totals"]["input_tokens"] == 600_000
    expected = (500_000 * 1 + 100_000 * 0.1 + 50_000 * 1) / 1e6
    assert data["totals"]["cost"]["USD"] == pytest.approx(expected)
    # Same figure as the task's own cost card.
    assert execution_view.cost(runtime.store.db, "T")["known_cost"] == pytest.approx(expected)
    assert data["by_model"][0]["provider"] == "codex_cli" and data["by_model"][0]["model"] == "gpt-x"
    assert data["by_task"][0]["id"] == "T" and data["by_task"][0]["title"] == "Execution task"
    assert data["daily"][-1]["day"] == time.strftime("%Y-%m-%d") and data["daily"][-1]["calls"] == 1
    assert data["progress"]["total"] == 1 and data["state_labels"]["running"] == "실행 중"


def test_session_member_rows_are_not_unknown_calls_in_task_cost(runtime):  # noqa: F811
    runtime.execute("T", "run-once", runner=Runner({"input_tokens": 10, "output_tokens": 1}))
    before = execution_view.cost(runtime.store.db, "T")
    runtime.store.event(
        "T", "RunnerUsage", {"attempt_id": "member", "phase_id": "tester", "provider": "codex_cli", "usage": {}, "selection": {"model": "gpt-x"}, "ai_call": False}
    )
    after = execution_view.cost(runtime.store.db, "T")
    assert after["unknown_calls"] == before["unknown_calls"] == 0
    assert usage_dashboard.summary(runtime.root, 30)["totals"]["calls"] == 1


def test_summary_rejects_unknown_periods_and_handles_unprepared_workspace(tmp_path):
    with pytest.raises(Exception, match="기간"):
        usage_dashboard.summary(tmp_path, 5)
    data = usage_dashboard.summary(tmp_path, 30)
    assert data["ready"] is False


def fetch(port, path, method="GET", body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, data


def test_usage_page_and_api_are_read_only(tmp_path):
    initialize(tmp_path)
    server = studio_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        for path in ("/usage", "/usage.js", "/usage.css"):
            assert fetch(port, path)[0] == 200
        status, body = fetch(port, "/api/usage/bootstrap?days=30")
        assert status == 200 and json.loads(body)["days"] == 30
        status, body = fetch(port, "/api/usage/summary?days=0")
        assert status == 200 and json.loads(body)["days"] == 0
        assert fetch(port, "/api/usage/summary?days=5")[0] == 400
        assert fetch(port, "/api/usage/summary?days=7&extra=1")[0] == 400
        assert fetch(port, "/api/usage/other")[0] == 400
        assert fetch(port, "/api/usage/summary", "POST", b"{}", {"Content-Type": "application/json"})[0] == 400
        # Cross-site reads are refused like every other page API.
        assert fetch(port, "/api/usage/summary", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_report_links_studio_planning_to_handed_off_tasks():
    calls = [
        usage_event("a1", 1_000_000),  # T1 execution
        {
            "studio_id": "S1",
            "policy_key": "studio",
            "created_at": 900.0,
            "payload": {"attempt_id": "studio-1", "phase_id": "studio:spec_requirements", "provider": "codex_cli", "usage": {"input_tokens": 500_000, "output_tokens": 50_000}, "selection": {"model": "gpt-x"}},
        },
    ]
    data = report(
        tasks("running", "planned"), calls, {"T1": PRICED, "studio": PRICED},
        studios={"S1": {"title": "결제 기능", "tasks": ["T1", "T2"]}, "S2": {"title": "사용 안 함", "tasks": []}},
    )
    assert data["totals"]["task_calls"] == 1 and data["totals"]["studio_calls"] == 1
    assert [r["id"] for r in data["by_task"]] == ["T1"]  # Studio calls are not tasks.
    (feature,) = data["by_studio"]  # S2 has no usage and no linked usage.
    assert feature["title"] == "결제 기능" and [t["id"] for t in feature["tasks"]] == ["T1", "T2"]
    assert feature["planning"]["calls"] == 1 and feature["execution"]["calls"] == 1
    assert feature["total"]["total_tokens"] == 550_000 + 1_100_000
    assert feature["total"]["cost"]["USD"] == pytest.approx(0.55 + 1.1)
    assert "studio:spec_requirements" in [r["role"] for r in data["by_role"]]


def test_summary_reads_studio_usage_and_handoff_links(runtime):  # noqa: F811
    import sqlite3

    from agent_dev.adapters.studio_store import SCHEMA
    from agent_dev.adapters.task_queries import TaskQueries

    runtime.execute("T", "run-once", runner=Runner({"input_tokens": 1000, "output_tokens": 100}))
    with sqlite3.connect(runtime.store.db.path) as db:
        db.execute(
            "INSERT INTO handoff_imports VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("IMP", "P", "S1", "s", "m", "b", "D", 1, "PLAN", "key", time.time()),
        )
    studio_db = TaskQueries(runtime.root).locations.studio_db
    studio_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(studio_db) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO projects(id,title,brief,created) VALUES('S1','결제 기능','기획',?)", (time.time(),))
        payload = {"id": "job", "step": "mock", "provider": "codex_cli", "model": "gpt-x", "usage": {"input_tokens": 2000, "output_tokens": 200}, "runner_seconds": 3.0}
        db.execute("INSERT INTO events(project_id,kind,payload,created) VALUES('S1','job.usage',?,?)", (json.dumps(payload), time.time()))
    data = usage_dashboard.summary(runtime.root, 30)
    assert data["totals"]["studio_calls"] == 1 and data["totals"]["task_calls"] == 1
    (feature,) = data["by_studio"]
    assert feature["title"] == "결제 기능" and [t["id"] for t in feature["tasks"]] == ["T"]
    # Studio calls use the project's current price book (POLICY prices codex_cli gpt-x).
    assert feature["planning"]["cost"]["USD"] == pytest.approx((2000 + 200) / 1e6)
    assert feature["total"]["total_tokens"] == 2200 + 1100


def test_recorded_studio_cost_is_used_instead_of_the_current_price_book():
    call = {
        "studio_id": "S1",
        "policy_key": "studio",
        "created_at": 10.0,
        "priced": {"cost": 0.25, "currency": "USD", "reason": "estimated"},
        "payload": {"attempt_id": "studio-1", "provider": "codex_cli", "usage": {"input_tokens": 1, "output_tokens": 1}, "selection": {"model": "gpt-x"}},
    }
    unpriced = dict(call, priced={"cost": None, "currency": None, "reason": "price_book_missing"}, payload=dict(call["payload"], attempt_id="studio-2"))
    data = report([], [call, unpriced], {"studio": PRICED})  # A price book added later changes nothing.
    assert data["totals"]["cost"] == {"USD": 0.25}
    assert data["cost"]["known_calls"] == 1 and data["cost"]["reasons"] == {"price_book_missing": 1}


FAKE_CLI = r'''#!{python}
import json, sys
from pathlib import Path
args = sys.argv[1:]
if "--help" in args or "--version" in args:
    print("Usage: fake [options] --model --output-format --output-last-message --ask-for-approval --admin-policy")
    sys.exit(0)
sys.stdin.read()
payload = Path({payload!r}).read_text(encoding="utf-8")
if {codex!r}:
    out = args[args.index("--output-last-message") + 1]
    Path(out).write_text(payload, encoding="utf-8")
    print(json.dumps({{"type": "thread.started", "thread_id": "fake-thread"}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 300}}}}))
else:
    print(json.dumps({{"type": "system", "subtype": "init"}}))
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": payload,
                      "session_id": "fake-session", "usage": {{"input_tokens": 1200, "output_tokens": 300}}}}))
'''


@pytest.mark.skipif(sys.platform == "win32", reason="the fake CLI is a shebang script")
@pytest.mark.parametrize("provider", ["claude_cli", "codex_cli"])
def test_cli_studio_generation_records_usage_with_the_queued_price(tmp_path, provider):
    import sqlite3
    from importlib.resources import files

    from agent_dev.adapters.project_setup import ProjectSetup
    from agent_dev.adapters.studio_generation import StudioJobs
    from agent_dev.adapters.studio_store import StudioStore
    from agent_dev.application.studio import Studio
    from tests.test_project_setup import config
    from tests.test_studio import BRIEF, specifications, wait_done

    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(specifications()), encoding="utf-8")
    script = tmp_path / ("fake-" + provider)
    script.write_text(
        FAKE_CLI.format(python=sys.executable, payload=str(payload), codex=provider == "codex_cli"), encoding="utf-8"
    )
    script.chmod(0o755)
    service = ProjectSetup(tmp_path)
    settings = config(tmp_path)
    settings["ai"]["provider"] = provider
    settings["ai"]["runners"][provider].update(enabled=True, command=str(script), model="fixture-model")
    settings["usage_policy"] = {
        "currency": "USD",
        "price_book": [{"provider": provider, "model": "fixture-model", "input_per_million": 1000.0, "cached_input_per_million": 100.0, "output_per_million": 2000.0}],
    }
    service.save(settings)
    studio = Studio(StudioStore(tmp_path), files("agent_dev.ui").joinpath("assets"))
    jobs = StudioJobs(studio, config_root=tmp_path)
    try:
        item = studio.create("CLI 기획", BRIEF)
        pid = item["project"]["id"]
        jobs.start(pid, item["project"]["version"], "specs")
        item = wait_done(studio, pid)
        assert item["jobs"][0]["state"] == "done", item["jobs"][0]
    finally:
        jobs.close()
    with sqlite3.connect(studio.store.path) as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='job.usage'")]
    assert len(rows) >= 2 and all(r["provider"] == provider and r["model"] == "fixture-model" for r in rows)
    if provider == "codex_cli":
        usage = {"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 300}
        cost = (1000 * 1000.0 + 200 * 100.0 + 300 * 2000.0) / 1e6  # Codex counts cached inside input.
    else:
        usage = {"input_tokens": 1200, "output_tokens": 300}
        cost = (1200 * 1000.0 + 300 * 2000.0) / 1e6
    assert all(r["usage"] == usage and r["cost"] == pytest.approx(cost) and r["currency"] == "USD" for r in rows)
    # Raising the price afterwards does not reprice what was already generated.
    settings = config(tmp_path)
    settings["usage_policy"]["price_book"][0]["output_per_million"] = 9999.0
    service.save(settings)
    data = usage_dashboard.summary(tmp_path, 30)
    assert data["totals"]["studio_calls"] == len(rows)
    assert data["totals"]["cost"]["USD"] == pytest.approx(cost * len(rows))


def test_claude_cache_tokens_are_counted_not_dropped():
    from agent_dev.runners.usage import claude_result

    stream = json.dumps({"type": "result", "result": "ok", "usage": {
        "input_tokens": 40, "cache_creation_input_tokens": 5000, "cache_read_input_tokens": 90000, "output_tokens": 700,
    }})
    assert claude_result(stream) == ("ok", {"input_tokens": 5040, "cached_input_tokens": 90000, "output_tokens": 700})
    # claude_cli is Anthropic style: cached reads are priced separately from input.
    from agent_dev.domain.usage_policy import call_cost

    policy = normalize_policy({"currency": "USD", "price_book": [{"provider": "claude_cli", "model": "m", "input_per_million": 3.0, "cached_input_per_million": 0.3, "output_per_million": 15.0}]})
    cost, _ = call_cost(policy, {"provider": "claude_cli", "model": "m", "usage": claude_result(stream)[1]})
    assert cost == pytest.approx((5040 * 3.0 + 90000 * 0.3 + 700 * 15.0) / 1e6)
