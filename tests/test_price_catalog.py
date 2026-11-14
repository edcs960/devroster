"""Automatic price list: conversion, history, lookup, storage, refresh and its routes.

No test reaches the public source: ``download`` is replaced or pointed at a local server.
"""

import contextlib
import http.server
import json
import os
import runpy
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_dev.adapters import price_catalog as store
from agent_dev.domain import price_catalog
from agent_dev.domain.errors import DomainError
from agent_dev.domain.usage_policy import (
    budget_pause_due,
    call_cost,
    estimate,
    normalize_policy,
    with_auto_prices,
)
from agent_dev.domain.usage_report import report
from agent_dev.ui.setup_server import make_server

DAY = 86400
T0 = 1_800_000_000  # 2027-01-15 UTC
USAGE = {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 100_000}


def entry(provider, input_cost, output_cost, mode="chat", **more):
    return {
        "litellm_provider": provider,
        "mode": mode,
        "input_cost_per_token": input_cost,
        "output_cost_per_token": output_cost,
        **more,
    }


def source(price=2e-6, models=25):
    """A source list shaped like the public file, large enough to pass the format check."""
    data = {f"gpt-test-{n}": entry("openai", 1e-6, 2e-6) for n in range(models)}
    data["gpt-main"] = entry("openai", price, 8e-6, cache_read_input_token_cost=5e-7)
    data["sample_spec"] = {"litellm_provider": "one of ...", "mode": "chat"}
    return data


def rows(*items):
    return [
        {
            "vendor": vendor,
            "model": model,
            "valid_from": valid_from,
            "input_per_million": rate,
            "cached_input_per_million": rate / 10,
            "output_per_million": rate * 4,
        }
        for vendor, model, valid_from, rate in items
    ]


def test_source_entries_become_rows_per_million_tokens():
    data = {
        "gpt-a": entry("openai", 2e-6, 8e-6, cache_read_input_token_cost=5e-7),
        "o-resp": entry("openai", 1e-6, 4e-6, mode="responses"),
        "claude-a": entry("anthropic", 3e-6, 15e-6),
        "gemini-a": entry("gemini", 9e-6, 9e-6),
        "gemini/gemini-a": entry("gemini", 1e-7, 4e-7),
        "xai/grok-a": entry("xai", 2e-6, 1e-5),
        # Left out: other vendors and modes, fine-tunes, nested names, unusable prices.
        "azure/gpt-a": entry("azure", 2e-6, 8e-6),
        "text-embed": entry("openai", 1e-7, 0, mode="embedding"),
        "ft:gpt-a": entry("openai", 2e-6, 8e-6),
        "gemini/tuned/gemini-a": entry("gemini", 1e-7, 4e-7),
        "gpt-no-output": {"litellm_provider": "openai", "mode": "chat", "input_cost_per_token": 1e-6},
        "gpt-negative": entry("openai", -1e-6, 8e-6),
        "gpt-flag": entry("openai", True, 8e-6),
        "gpt-broken": entry("openai", 5.0, 8e-6),
        "gpt-nan": entry("openai", float("nan"), 8e-6),
        "gpt-text": "not an entry",
    }
    result = {(row["vendor"], row["model"]): row for row in price_catalog.from_source(data)}
    assert sorted(result) == [
        ("anthropic", "claude-a"),
        ("gemini", "gemini-a"),
        ("openai", "gpt-a"),
        ("openai", "o-resp"),
        ("xai", "grok-a"),
    ]
    assert result[("openai", "gpt-a")] == {
        "vendor": "openai",
        "model": "gpt-a",
        "input_per_million": 2.0,
        "cached_input_per_million": 0.5,
        "output_per_million": 8.0,
    }
    # The vendor-prefixed entry is the vendor's own API; a missing cached price stays unknown.
    assert result[("gemini", "gemini-a")]["input_per_million"] == 0.1
    assert result[("anthropic", "claude-a")]["cached_input_per_million"] is None
    assert price_catalog.from_source(["not", "a", "mapping"]) == []


def test_stored_rows_are_validated():
    good = rows(("openai", "gpt-a", "2026-01-01", 2.0))[0]
    bad = [
        dict(good, vendor="azure"),
        dict(good, model=""),
        dict(good, valid_from="2026/01/01"),
        dict(good, valid_from=None),
        dict(good, input_per_million=-1),
        dict(good, output_per_million=None),
        dict(good, cached_input_per_million="0.1"),
        dict(good, input_per_million=float("inf")),
        "row",
    ]
    assert price_catalog.valid_rows([*bad, dict(good, extra="dropped")]) == [good]
    assert price_catalog.valid_rows({"rows": []}) == []


def test_history_keeps_the_price_of_past_calls():
    first = price_catalog.merge([], price_catalog.from_source(source()), "2026-11-01")
    main = [row for row in first if row["model"] == "gpt-main"]
    # No earlier price is known for a model seen for the first time.
    assert [row["valid_from"] for row in main] == ["1970-01-01"]
    assert price_catalog.merge(first, price_catalog.from_source(source()), "2026-11-02") == first

    second = price_catalog.merge(first, price_catalog.from_source(source(4e-6)), "2026-12-01")
    main = [row for row in second if row["model"] == "gpt-main"]
    assert [(row["valid_from"], row["input_per_million"]) for row in main] == [
        ("1970-01-01", 2.0),
        ("2026-12-01", 4.0),
    ]
    before = price_catalog.price(second, "openai_api", "gpt-main", 1_795_000_000)  # 2026-11-18
    after = price_catalog.price(second, "openai_api", "gpt-main", T0)
    assert (before["input_per_million"], after["input_per_million"]) == (2.0, 4.0)

    # A second change on the same day corrects that day's row instead of adding one.
    third = price_catalog.merge(second, price_catalog.from_source(source(5e-6)), "2026-12-01")
    assert [r["input_per_million"] for r in third if r["model"] == "gpt-main"] == [2.0, 5.0]

    # A model the source dropped keeps its rows.
    fewer = source()
    del fewer["gpt-main"]
    assert any(
        r["model"] == "gpt-main"
        for r in price_catalog.merge(third, price_catalog.from_source(fewer), "2026-12-02")
    )


def test_history_is_bounded_per_model():
    history = []
    for day in range(1, 10):
        history = price_catalog.merge(
            history, price_catalog.from_source(source(day * 1e-6)), f"2026-11-{day:02d}"
        )
    main = [row for row in history if row["model"] == "gpt-main"]
    assert len(main) == price_catalog.HISTORY and main[-1]["input_per_million"] == 9.0


def test_lookup_covers_api_providers_only():
    table = rows(
        ("openai", "gpt-a", "1970-01-01", 2.0),
        ("gemini", "gemini-a", "1970-01-01", 1.0),
        ("anthropic", "claude-a", "2026-06-01", 3.0),
    )
    assert price_catalog.price(table, "openai_api", "gpt-a", T0)["input_per_million"] == 2.0
    assert price_catalog.price(table, "gemini_api", "models/gemini-a", T0)["model"] == "gemini-a"
    assert price_catalog.price(table, "gemini_api", "gpt-a", T0) is None
    # Not yet valid on that day.
    assert price_catalog.price(table, "anthropic_api", "claude-a", 1_767_225_600) is None
    assert price_catalog.price(table, "anthropic_api", "claude-a", None)["input_per_million"] == 3.0
    # A subscription CLI and the other API tools are never priced from this list.
    for provider in ("codex_cli", "claude_cli", "gemini_cli", "exaone_api", "compatible_api"):
        assert price_catalog.price(table, provider, "gpt-a", T0) is None
    assert price_catalog.price(table, "openai_api", None, T0) is None
    assert price_catalog.models(table) == 3


BOOK = {
    "currency": "USD",
    "alert_thresholds": [],
    "estimated_budget": 10.0,
    "price_book": [
        {"provider": "openai_api", "model": "gpt-a", "input_per_million": 1.0, "cached_input_per_million": 0.1, "output_per_million": 1.0},
    ],
}
TABLE = rows(("openai", "gpt-a", "1970-01-01", 2.0), ("openai", "gpt-b", "1970-01-01", 2.0))


def call(provider, model, **more):
    return {"provider": provider, "model": model, "usage": USAGE, "created_at": T0, **more}


def test_the_owners_price_wins_over_the_automatic_list():
    policy = with_auto_prices(normalize_policy(BOOK), TABLE)
    assert call_cost(policy, call("openai_api", "gpt-a")) == (pytest.approx(1.1), "estimated")
    assert call_cost(policy, call("openai_api", "gpt-b")) == (pytest.approx(2.8), "estimated_auto")
    assert call_cost(policy, call("openai_api", "gpt-c")) == (None, "price_missing")
    assert call_cost(policy, call("codex_cli", "gpt-b")) == (None, "price_missing")

    summary = estimate(policy, [call("openai_api", "gpt-a"), call("openai_api", "gpt-b"), call("codex_cli", "gpt-b")])
    assert (summary["known_calls"], summary["auto_calls"], summary["unknown_calls"]) == (2, 1, 1)
    assert summary["known_cost"] == pytest.approx(3.9) and summary["budget_ratio"] == pytest.approx(0.39)
    assert "자동 가격표" in summary["note"]
    assert "자동 가격표" not in estimate(policy, [call("openai_api", "gpt-a")])["note"]


def test_a_won_project_without_an_exchange_rate_keeps_only_its_own_price_book():
    policy = normalize_policy(dict(BOOK, currency="KRW"))
    assert "exchange_rate" not in policy
    assert with_auto_prices(policy, TABLE) is policy
    assert call_cost(with_auto_prices(policy, TABLE), call("openai_api", "gpt-b")) == (None, "price_missing")


def test_a_won_project_shows_dollar_prices_through_its_exchange_rate():
    won = dict(BOOK, currency="KRW", exchange_rate=1400, estimated_budget=10000.0)
    policy = with_auto_prices(normalize_policy(won), TABLE)
    # The owner's own row is already in won, so it is not converted.
    assert call_cost(policy, call("openai_api", "gpt-a")) == (pytest.approx(1.1), "estimated")
    assert call_cost(policy, call("openai_api", "gpt-b")) == (pytest.approx(2.8 * 1400), "estimated_auto")
    assert call_cost(policy, call("codex_cli", "gpt-b")) == (None, "price_missing")

    summary = estimate(policy, [call("openai_api", "gpt-a"), call("openai_api", "gpt-b")])
    assert summary["currency"] == "KRW" and summary["known_cost"] == pytest.approx(3921.1)
    assert summary["budget_ratio"] == pytest.approx(0.39211)
    assert "1 USD = 1,400 KRW" in summary["note"]
    assert "환율" not in estimate(policy, [call("openai_api", "gpt-a")])["note"]

    # The budget is in won, and the converted cost counts toward it.
    pausing = with_auto_prices(
        normalize_policy(dict(won, estimated_budget=3000.0, pause_at_budget=True)), TABLE
    )
    assert budget_pause_due(pausing, estimate(pausing, [call("openai_api", "gpt-b")])) is True
    assert budget_pause_due(pausing, estimate(pausing, [call("openai_api", "gpt-a")])) is False


@pytest.mark.parametrize("rate", [0, -1, 1_000_001, "1400", True, float("nan"), float("inf")])
def test_a_bad_exchange_rate_is_refused(rate):
    with pytest.raises(DomainError):
        normalize_policy(dict(BOOK, currency="KRW", exchange_rate=rate))


def test_the_exchange_rate_is_kept_only_where_it_applies():
    # A dollar project needs none, and a policy without one reads exactly as before.
    assert normalize_policy(dict(BOOK, exchange_rate=1400)) == normalize_policy(BOOK)
    assert normalize_policy(dict(BOOK, currency="KRW", exchange_rate=None)) == normalize_policy(
        dict(BOOK, currency="KRW")
    )
    # The rate alone is a cost setting: a won project then needs no price book of its own.
    policy = normalize_policy({"currency": "KRW", "exchange_rate": 1385.5})
    assert policy["exchange_rate"] == 1385.5 and policy["price_book"] == []
    summary = estimate(with_auto_prices(policy, TABLE), [call("openai_api", "gpt-b")])
    assert summary["known_cost"] == pytest.approx(2.8 * 1385.5)
    assert "1 USD = 1,385.5 KRW" in summary["note"]


def test_a_project_without_cost_settings_prices_api_calls_only():
    assert with_auto_prices(None, []) is None
    policy = with_auto_prices(None, TABLE)
    assert policy["currency"] == "USD" and policy["pause_at_budget"] is False

    # Only subscription CLI calls: nothing is priced, so it still reads "not configured".
    summary = estimate(policy, [call("codex_cli", "gpt-b"), call("claude_cli", "claude-a")])
    assert summary["configured"] is False and summary["unknown_calls"] == 2
    assert call_cost(policy, call("codex_cli", "gpt-b")) == (None, "price_book_missing")

    summary = estimate(policy, [call("openai_api", "gpt-b"), call("codex_cli", "gpt-b")])
    assert summary["configured"] is True and summary["currency"] == "USD"
    assert (summary["known_calls"], summary["auto_calls"], summary["unknown_calls"]) == (1, 1, 1)
    assert summary["reasons"] == {"price_book_missing": 1} and summary["budget"] is None


def test_the_usage_report_counts_automatically_priced_calls():
    policy = with_auto_prices(None, TABLE)
    task = {"id": "t1", "title": "작업", "state": "running", "updated_at": T0}

    def recorded(provider, attempt):
        payload = {"provider": provider, "selection": {"model": "gpt-b"}, "usage": USAGE, "attempt_id": attempt}
        return {"task_id": "t1", "payload": payload, "created_at": T0}

    studio = {
        "studio_id": "s1",
        "payload": {"provider": "openai_api", "selection": {"model": "gpt-b"}, "usage": USAGE, "attempt_id": "s"},
        "created_at": T0,
        "priced": {"cost": 0.5, "currency": "USD", "reason": "estimated_auto"},
    }
    data = report([task], [recorded("openai_api", "a"), recorded("codex_cli", "b"), studio], {"t1": policy})
    cost = data["cost"]
    assert (cost["known_calls"], cost["auto_calls"], cost["unknown_calls"]) == (2, 2, 1)
    assert cost["reasons"] == {"price_book_missing": 1} and cost["unpriced_tasks"] == 1
    assert data["totals"]["cost"] == {"USD": pytest.approx(3.3)}
    assert "자동 가격표" in cost["note"] and "환율" not in cost["note"]
    assert "자동 가격표" not in report([task], [recorded("codex_cli", "b")], {"t1": policy})["cost"]["note"]

    won = with_auto_prices(normalize_policy({"currency": "KRW", "exchange_rate": 1400}), TABLE)
    data = report([task], [recorded("openai_api", "a")], {"t1": won})
    assert data["totals"]["cost"] == {"KRW": pytest.approx(3920)}
    assert "환율" in data["cost"]["note"]


def test_the_bundled_list_is_valid_and_carries_its_license():
    data = json.loads(store.BUNDLED.read_text(encoding="utf-8"))
    bundled, taken_on = store.bundled()
    assert bundled == data["rows"], "every bundled row must pass validation unchanged"
    assert price_catalog.models(bundled) >= store.MIN_MODELS
    assert {row["vendor"] for row in bundled} == set(price_catalog.VENDORS.values())
    assert price_catalog.valid_rows([dict(bundled[0], valid_from=taken_on)])
    assert data["source"] == price_catalog.SOURCE_URL
    license_text = store.BUNDLED.with_name("price_catalog-LICENSE.txt").read_text(encoding="utf-8")
    assert "MIT License" in license_text and "Berri AI" in license_text


def test_before_any_refresh_the_bundled_list_is_used():
    status = store.status()
    assert status["auto_refresh"] is False and status["fetched_at"] is None
    assert status["bundled_on"] == store.bundled()[1] and status["models"] >= store.MIN_MODELS
    assert (status["source"], status["currency"], status["last_error"]) == ("LiteLLM", "USD", None)
    assert not store.path().exists(), "reading the status writes nothing"
    assert store.resolve(None)["auto_prices"] == store.bundled()[0]
    assert store.resolve(dict(BOOK, currency="KRW")).get("auto_prices") is None
    assert store.resolve(dict(BOOK, currency="KRW", exchange_rate=1400))["auto_prices"] == store.bundled()[0]


def test_refresh_stores_the_fetched_prices_as_history():
    status = store.refresh(lambda: source(), now=T0)
    assert status["fetched_at"] == T0 and status["bundled_on"] is None and status["last_error"] is None
    assert status["models"] == price_catalog.models(store.bundled()[0]) + 26
    if os.name == "posix":
        assert store.path().stat().st_mode & 0o777 == 0o600
    saved = json.loads(store.path().read_text(encoding="utf-8"))
    assert saved["version"] == store.VERSION and saved["auto_refresh"] is False

    store.refresh(lambda: source(4e-6), now=T0 + 30 * DAY)
    policy = store.resolve(None)
    old = call_cost(policy, {"provider": "openai_api", "model": "gpt-main", "usage": USAGE, "created_at": T0})
    new = call_cost(policy, {"provider": "openai_api", "model": "gpt-main", "usage": USAGE, "created_at": T0 + 31 * DAY})
    assert old == (pytest.approx(2.8), "estimated_auto") and new == (pytest.approx(4.8), "estimated_auto")
    # Models of the bundled list stay priced after a refresh that does not list them.
    assert all(row in policy["auto_prices"] for row in store.bundled()[0])


def test_a_failed_refresh_keeps_the_previous_list():
    store.refresh(lambda: source(), now=T0)
    before = store.rows()

    def offline():
        raise DomainError(store.ErrorCode.EXTERNAL_TRANSIENT, "모델 가격 목록을 가져오지 못했습니다.")

    with pytest.raises(DomainError):
        store.refresh(offline, now=T0 + DAY)
    with pytest.raises(DomainError, match="형식이 바뀌어"):
        store.refresh(lambda: {"gpt-a": entry("openai", 1e-6, 2e-6)}, now=T0 + 2 * DAY)
    status = store.status()
    assert store.rows() == before and status["fetched_at"] == T0
    assert "형식이 바뀌어" in status["last_error"]
    # The next success clears the error.
    assert store.refresh(lambda: source(), now=T0 + 3 * DAY)["last_error"] is None


def test_the_daily_refresh_runs_only_when_turned_on():
    calls = []

    def fetch():
        calls.append(1)
        return source()

    assert store.refresh_if_due(fetch, now=T0) is False and not calls
    with pytest.raises(DomainError):
        store.configure("yes")
    assert store.configure(True)["auto_refresh"] is True
    assert store.refresh_if_due(fetch, now=T0) is True
    assert store.refresh_if_due(fetch, now=T0 + DAY - 60) is False
    assert store.refresh_if_due(fetch, now=T0 + DAY + 60) is True
    assert len(calls) == 2

    def offline():
        calls.append(1)
        raise DomainError(store.ErrorCode.EXTERNAL_TRANSIENT, "오프라인")

    later = T0 + 3 * DAY
    assert store.refresh_if_due(offline, now=later) is False and len(calls) == 3
    # A failure is retried after an hour, not on every check.
    assert store.refresh_if_due(offline, now=later + 600) is False and len(calls) == 3
    assert store.refresh_if_due(fetch, now=later + store.RETRY_SECONDS + 1) is True
    assert store.configure(False)["auto_refresh"] is False
    assert store.refresh_if_due(fetch, now=later + 10 * DAY) is False
    assert store.status()["fetched_at"] == later + store.RETRY_SECONDS + 1, "the setting keeps the list"


def test_an_unreadable_stored_file_falls_back_to_the_bundled_list():
    store.path().parent.mkdir(parents=True, exist_ok=True)
    store.path().write_text("{not json", encoding="utf-8")
    assert store.rows() == store.bundled()[0] and store.status()["auto_refresh"] is False
    store.path().write_text(json.dumps({"auto_refresh": True, "rows": [{"vendor": "x"}]}), encoding="utf-8")
    assert store.rows() == store.bundled()[0] and store.status()["auto_refresh"] is True


def test_the_background_refresh_stops_with_its_event(monkeypatch):
    ran, stop = threading.Event(), threading.Event()
    monkeypatch.setattr(store, "refresh_if_due", ran.set)
    assert store.start_auto_refresh(stop, interval=0.01) is stop
    assert ran.wait(5)
    stop.set()


@contextlib.contextmanager
def source_server(status, body, headers=()):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    instance = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}/prices.json", seen
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def test_download_reads_one_address_and_sends_no_credentials(monkeypatch):
    with source_server(200, json.dumps(source()).encode()) as (url, seen):
        monkeypatch.setattr(price_catalog, "SOURCE_URL", url)
        assert "gpt-main" in store.download(timeout=5)
        assert not {"authorization", "x-api-key"} & {name.lower() for name in seen[0]}
    with source_server(200, b"<html>") as (url, _):
        monkeypatch.setattr(price_catalog, "SOURCE_URL", url)
        with pytest.raises(DomainError, match="가져오지 못했습니다"):
            store.download(timeout=5)
    with source_server(200, json.dumps(source()).encode()) as (target, seen):
        with source_server(302, b"", [("Location", target)]) as (url, _):
            monkeypatch.setattr(price_catalog, "SOURCE_URL", url)
            with pytest.raises(DomainError):
                store.download(timeout=5)
        assert not seen, "a redirect is not followed"
    monkeypatch.setattr(store, "LIMIT", 10)
    with source_server(200, json.dumps(source()).encode()) as (url, _):
        monkeypatch.setattr(price_catalog, "SOURCE_URL", url)
        with pytest.raises(DomainError):
            store.download(timeout=5)


def request(base, path, payload=None, token=""):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "X-Setup-Token": token},
    )
    try:
        response = urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, json.loads(response.read())


def test_price_routes(tmp_path, monkeypatch):
    instance = make_server(tmp_path / "project", 0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(instance.server_port)
    try:
        token = request(base, "/api/setup/bootstrap")[1]["token"]
        status, data = request(base, "/api/setup/prices")
        assert status == 200 and data["auto_refresh"] is False and data["models"] >= store.MIN_MODELS
        # Changing the setting or fetching needs the page token.
        assert request(base, "/api/setup/prices", {"auto_refresh": True})[0] == 403
        assert request(base, "/api/setup/prices/refresh", {})[0] == 403
        assert store.status()["auto_refresh"] is False

        assert request(base, "/api/setup/prices", {"auto_refresh": "yes"}, token)[0] == 400
        status, data = request(base, "/api/setup/prices", {"auto_refresh": True}, token)
        assert status == 200 and data["auto_refresh"] is True

        monkeypatch.setattr(store, "download", lambda: source())
        status, data = request(base, "/api/setup/prices/refresh", {}, token)
        assert status == 200 and data["fetched_at"] and data["bundled_on"] is None

        def offline():
            raise DomainError(store.ErrorCode.EXTERNAL_TRANSIENT, "모델 가격 목록을 가져오지 못했습니다.")

        monkeypatch.setattr(store, "download", offline)
        status, data = request(base, "/api/setup/prices/refresh", {}, token)
        assert status == 400 and "가져오지 못했습니다" in data["error"]
        assert request(base, "/api/setup/prices")[1]["last_error"] == data["error"]
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def test_the_bundled_list_can_be_rebuilt_from_a_source_file(tmp_path, monkeypatch, capsys):
    downloaded = tmp_path / "source.json"
    downloaded.write_text(json.dumps(source()), encoding="utf-8")
    target = tmp_path / "price_catalog.json"
    monkeypatch.setattr(store, "BUNDLED", target)
    script = Path(__file__).resolve().parents[1] / "scripts" / "update_price_catalog.py"
    runpy.run_path(str(script))["main"](["update_price_catalog.py", str(downloaded)])
    assert "26 models" in capsys.readouterr().out
    data = json.loads(target.read_text(encoding="utf-8"))
    assert price_catalog.valid_rows(data["rows"]) == data["rows"] and len(data["rows"]) == 26
    assert all(row["valid_from"] == price_catalog.EPOCH for row in data["rows"])


def test_a_cache_cleared_by_another_thread_mid_read_still_returns_the_list(monkeypatch):
    class Racing(dict):
        """Another thread (a settings save, an automatic refresh) clears the cache right after a lookup."""

        def get(self, key, default=None):
            value = super().get(key, default)
            self.clear()
            return value

    store.configure(True)
    monkeypatch.setattr(store, "_cache", Racing())
    first = store.status()
    assert store.status() == first and first["auto_refresh"] is True
