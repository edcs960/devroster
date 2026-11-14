"""Price book validation and estimated cost (CO-01): unknown is never zero."""

import pytest

from agent_dev.domain.errors import DomainError
from agent_dev.domain.usage_policy import call_cost, crossed_thresholds, estimate, normalize_policy

POLICY = {
    "currency": "USD",
    "alert_thresholds": [0.5, 0.8],
    "estimated_budget": 1.0,
    "price_book": [
        {"provider": "codex_cli", "model": "gpt-x", "input_per_million": 2.0, "cached_input_per_million": 0.5, "output_per_million": 8.0, "valid_from": "2026-01-01"},
        {"provider": "codex_cli", "model": "gpt-x", "input_per_million": 4.0, "cached_input_per_million": 1.0, "output_per_million": 16.0, "valid_from": "2026-06-01"},
        {"provider": "claude_cli", "model": "claude-y", "input_per_million": 3.0, "cached_input_per_million": 0.3, "output_per_million": 15.0},
    ],
}
JAN = 1767225600 + 86400 * 10  # 2026-01-11 UTC
JUL = 1782864000  # 2026-07-01 UTC


def policy():
    return normalize_policy(POLICY)


@pytest.mark.parametrize(
    "change",
    [
        {"currency": "EUR"},
        {"alert_thresholds": [1.5]},
        {"alert_thresholds": [0.5], "estimated_budget": None},
        {"estimated_budget": -1},
        {"estimated_budget": float("inf")},
        {"price_book": [POLICY["price_book"][0], POLICY["price_book"][0]]},
        {"price_book": [dict(POLICY["price_book"][0], output_per_million=None)]},
        {"price_book": [dict(POLICY["price_book"][0], valid_from="2026/01/01")]},
        {"unknown": 1},
    ],
)
def test_invalid_policies_are_rejected(change):
    with pytest.raises(DomainError):
        normalize_policy({**POLICY, **change})


def test_openai_style_cached_input_is_charged_once():
    cost, reason = call_cost(policy(), {"provider": "codex_cli", "model": "gpt-x", "created_at": JAN, "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 400_000, "output_tokens": 100_000}})
    assert reason == "estimated"
    assert cost == pytest.approx(600_000 * 2 / 1e6 + 400_000 * 0.5 / 1e6 + 100_000 * 8 / 1e6)


def test_anthropic_style_cached_input_is_separate():
    cost, _ = call_cost(policy(), {"provider": "claude_cli", "model": "claude-y", "usage": {"input_tokens": 1000, "cached_input_tokens": 5000, "output_tokens": 100}})
    assert cost == pytest.approx((1000 * 3 + 5000 * 0.3 + 100 * 15) / 1e6)


def test_versioned_price_follows_call_date():
    usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    early, _ = call_cost(policy(), {"provider": "codex_cli", "model": "gpt-x", "created_at": JAN, "usage": usage})
    late, _ = call_cost(policy(), {"provider": "codex_cli", "model": "gpt-x", "created_at": JUL, "usage": usage})
    assert (early, late) == (2.0, 4.0)


def test_missing_usage_model_or_price_stays_unknown_and_measured_zero_is_zero():
    calls = [
        {"call_id": "a", "provider": "codex_cli", "model": "gpt-x", "created_at": JAN, "usage": {}},
        {"call_id": "b", "provider": "codex_cli", "model": "", "usage": {"input_tokens": 1, "output_tokens": 1}},
        {"call_id": "c", "provider": "gemini_cli", "model": "g", "usage": {"input_tokens": 1, "output_tokens": 1}},
        {"call_id": "d", "provider": "codex_cli", "model": "gpt-x", "created_at": JAN, "usage": {"input_tokens": 0, "output_tokens": 0}},
        {"call_id": "d", "provider": "codex_cli", "model": "gpt-x", "created_at": JAN, "usage": {"input_tokens": 10**6, "output_tokens": 0}},
    ]
    summary = estimate(policy(), calls)
    assert summary["known_calls"] == 1 and summary["known_cost"] == 0
    assert summary["unknown_calls"] == 3
    assert summary["reasons"] == {"usage_unknown": 1, "model_unknown": 1, "price_missing": 1}
    assert "추정치" in summary["note"]


def test_without_policy_cost_is_not_configured():
    summary = estimate(None, [{"call_id": "x"}])
    assert summary["configured"] is False and summary["known_cost"] is None


def test_thresholds_cross_from_estimated_ratio():
    summary = estimate(policy(), [{"call_id": "a", "provider": "codex_cli", "model": "gpt-x", "created_at": JAN, "usage": {"input_tokens": 300_000, "output_tokens": 0}}])
    assert summary["budget_ratio"] == pytest.approx(0.6)
    assert crossed_thresholds(policy(), summary) == [0.5]


def test_setup_saves_normalized_policy_and_clears_with_null(tmp_path):
    from agent_dev.adapters.project_setup import ProjectSetup

    root = tmp_path / "project"
    value = ProjectSetup(root).read()
    value["create_directories"] = True
    value["usage_policy"] = {"currency": "KRW", "alert_thresholds": [0.8, 0.5], "estimated_budget": 1000, "price_book": [{"provider": "codex_cli", "model": "gpt-x", "input_per_million": 2, "cached_input_per_million": None, "output_per_million": 8}]}
    ProjectSetup(root).save(value)
    _, _, profile = ProjectSetup(root).current_profile()
    assert profile["usage_policy"]["alert_thresholds"] == [0.5, 0.8]
    assert profile["usage_policy"]["price_book"][0]["valid_from"] == "1970-01-01"
    loaded = ProjectSetup(root).read()
    assert loaded["usage_policy"]["currency"] == "KRW"
    assert "exchange_rate" not in loaded["usage_policy"]
    loaded["usage_policy"]["exchange_rate"] = 1400
    ProjectSetup(root).save(loaded)
    loaded = ProjectSetup(root).read()
    assert loaded["usage_policy"]["exchange_rate"] == 1400
    bad = ProjectSetup(root).read()
    bad["usage_policy"] = {"alert_thresholds": [0.5]}
    with pytest.raises(DomainError):
        ProjectSetup(root).save(bad)
    loaded["usage_policy"] = None
    ProjectSetup(root).save(loaded)
    _, _, profile = ProjectSetup(root).current_profile()
    assert "usage_policy" not in profile


def test_provider_cache_contracts_and_partial_measurements():
    book = normalize_policy({"price_book": [
        {"provider": "gemini_cli", "model": "g", "input_per_million": 1.0, "cached_input_per_million": 0.25, "output_per_million": 2.0},
        {"provider": "anthropic_api", "model": "c", "input_per_million": 3.0, "output_per_million": 15.0},
        {"provider": "local_runner", "model": "x", "input_per_million": 1.0, "output_per_million": 1.0},
    ]})
    gemini, _ = call_cost(book, {"provider": "gemini_cli", "model": "g", "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 10}})
    assert gemini == pytest.approx((600 * 1.0 + 400 * 0.25 + 10 * 2.0) / 1e6)
    assert call_cost(book, {"provider": "anthropic_api", "model": "c", "usage": {"input_tokens": 1, "cached_input_tokens": 5, "output_tokens": 1}}) == (None, "cached_price_missing")
    assert call_cost(book, {"provider": "local_runner", "model": "x", "usage": {"input_tokens": 1, "output_tokens": 1}}) == (None, "provider_cache_contract_unknown")
    assert call_cost(book, {"provider": "gemini_cli", "model": "g", "usage": {"input_tokens": 5}}) == (None, "usage_unknown")
