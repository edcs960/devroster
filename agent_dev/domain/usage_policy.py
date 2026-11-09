"""User-entered price book and budget alerts (CO-01).

Prices are data the project owner types in; nothing here fetches prices,
converts currencies or reads subscription balances. Absent measurements stay
unknown (never zero), cached input is charged once according to how each
provider reports it, and budget alerts are estimates. Stopping is opt-in: with
``pause_at_budget`` a task pauses before its next AI call once the estimate reaches the
budget, and continues only when the user resumes it (each resume allows one more budget).
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime

from agent_dev.domain.errors import DomainError, ErrorCode

CURRENCIES = {"USD", "KRW"}
RATE_KEYS = ("input_per_million", "cached_input_per_million", "output_per_million")
# Providers whose input_tokens already include cached_input_tokens (OpenAI style).
CACHED_INSIDE_INPUT = {
    "codex_cli", "openai_api", "xai_api", "compatible_api", "gemini_cli", "gemini_api"
}
# Providers that report cached reads separately from input_tokens (Anthropic style).
CACHED_SEPARATE = {"claude_cli", "anthropic_api"}


def _fail(message):
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def _rate(value, label):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        _fail(f"{label}은 0 이상의 숫자여야 합니다.")
    return float(value)


def normalize_policy(value):
    """Validated usage_policy profile data (or None when not configured)."""
    if value in (None, {}):
        return None
    if not isinstance(value, dict) or set(value) - {"currency", "alert_thresholds", "estimated_budget", "pause_at_budget", "price_book"}:
        _fail("비용 설정 항목을 확인해주세요.")
    currency = value.get("currency", "USD")
    if currency not in CURRENCIES:
        _fail("통화는 USD 또는 KRW만 사용할 수 있습니다.")
    thresholds = value.get("alert_thresholds", [])
    if not isinstance(thresholds, list) or len(thresholds) > 10:
        _fail("예산 알림 비율을 확인해주세요.")
    clean = []
    for item in thresholds:
        rate = _rate(item, "예산 알림 비율")
        if rate is None or not 0 < rate <= 1:
            _fail("예산 알림 비율은 0보다 크고 1 이하여야 합니다.")
        clean.append(rate)
    budget = _rate(value.get("estimated_budget"), "예상 예산")
    if clean and budget is None:
        _fail("예산 알림을 쓰려면 예상 예산을 입력해주세요.")
    pause = value.get("pause_at_budget", False)
    if not isinstance(pause, bool):
        _fail("예산 도달 시 일시정지 설정을 확인해주세요.")
    if pause and not budget:
        _fail("예산 도달 시 일시정지를 쓰려면 0보다 큰 예상 예산을 입력해주세요.")
    book = value.get("price_book", [])
    if not isinstance(book, list) or len(book) > 100:
        _fail("가격표는 100개까지 등록할 수 있습니다.")
    rows, seen = [], set()
    for row in book:
        if not isinstance(row, dict) or set(row) - {"provider", "model", "valid_from", *RATE_KEYS}:
            _fail("가격표 항목을 확인해주세요.")
        provider, model, valid_from = row.get("provider"), row.get("model"), row.get("valid_from", "1970-01-01")
        if not isinstance(provider, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", provider):
            _fail("가격표의 AI 도구 이름을 확인해주세요.")
        if not isinstance(model, str) or not 1 <= len(model) <= 120:
            _fail("가격표의 모델 이름을 확인해주세요.")
        try:
            date.fromisoformat(valid_from)
        except (TypeError, ValueError):
            _fail("가격 적용 시작일은 YYYY-MM-DD 형식이어야 합니다.")
        key = (provider, model, valid_from)
        if key in seen:
            _fail(f"같은 모델·적용일의 가격이 중복되었습니다: {provider} {model} {valid_from}")
        seen.add(key)
        rates = {k: _rate(row.get(k), "단가") for k in RATE_KEYS}
        if rates["input_per_million"] is None or rates["output_per_million"] is None:
            _fail(f"{model}의 입력·출력 단가를 입력해주세요.")
        rows.append({"provider": provider, "model": model, "valid_from": valid_from, **rates})
    return {
        "currency": currency,
        "alert_thresholds": sorted(set(clean)),
        "estimated_budget": budget,
        "pause_at_budget": pause,
        "price_book": sorted(rows, key=lambda r: (r["provider"], r["model"], r["valid_from"])),
    }


def _price(policy, provider, model, when):
    day = datetime.fromtimestamp(when, UTC).date().isoformat() if isinstance(when, (int, float)) else None
    candidates = [
        row for row in policy["price_book"]
        if row["provider"] == provider and row["model"] == model and (day is None or row["valid_from"] <= day)
    ]
    return candidates[-1] if candidates else None


def call_cost(policy, call):
    """(cost or None, reason) for one recorded call; never treats missing as 0."""
    usage = call.get("usage") if isinstance(call.get("usage"), dict) else {}
    provider, model = call.get("provider") or "", call.get("model") or ""
    if not provider or not model:
        return None, "model_unknown"
    price = _price(policy, provider, model, call.get("created_at"))
    if price is None:
        return None, "price_missing"
    if "input_tokens" not in usage or "output_tokens" not in usage:
        return None, "usage_unknown"
    cached = usage.get("cached_input_tokens")
    if provider in CACHED_INSIDE_INPUT:
        cached = cached or 0
        uncached = max(0, usage["input_tokens"] - cached)
    elif provider in CACHED_SEPARATE:
        uncached = usage["input_tokens"]
        if cached is None:
            cached = 0  # Cached reads were not reported; cost covers reported tokens only.
    else:
        return None, "provider_cache_contract_unknown"
    cached_rate = price["cached_input_per_million"]
    if cached and cached_rate is None:
        return None, "cached_price_missing"
    cost = (
        uncached * price["input_per_million"]
        + (cached or 0) * (cached_rate or 0)
        + usage["output_tokens"] * price["output_per_million"]
    ) / 1_000_000
    return cost, "estimated"


def estimate(policy, calls):
    """Known subtotal plus unknown call count; duplicate call IDs counted once."""
    if policy is None:
        return {"configured": False, "currency": None, "known_cost": None, "known_calls": 0, "unknown_calls": len(calls), "reasons": {}, "by_model": []}
    seen, known, unknown, reasons, models = set(), 0.0, 0, {}, {}
    known_calls = 0
    for call in calls:
        call_id = call.get("call_id")
        if call_id:
            if call_id in seen:
                continue
            seen.add(call_id)
        cost, reason = call_cost(policy, call)
        if cost is None:
            unknown += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        known += cost
        known_calls += 1
        key = (call.get("provider"), call.get("model"), call.get("source", "canonical"))
        models[key] = models.get(key, 0.0) + cost
    budget = policy["estimated_budget"]
    return {
        "configured": True,
        "currency": policy["currency"],
        "known_cost": round(known, 6),
        "known_calls": known_calls,
        "unknown_calls": unknown,
        "reasons": reasons,
        "budget": budget,
        "budget_ratio": round(known / budget, 6) if budget else None,
        "by_model": [
            {"provider": p, "model": m, "source": s, "cost": round(c, 6)} for (p, m, s), c in sorted(models.items(), key=lambda x: (str(x[0][0]), str(x[0][1]), str(x[0][2])))
        ],
        "note": "사용자가 입력한 가격표로 계산한 추정치입니다. 실제 청구액·구독 잔여량이 아닙니다.",
    }


def crossed_thresholds(policy, summary):
    if policy is None or not policy["estimated_budget"] or summary.get("budget_ratio") is None:
        return []
    return [t for t in policy["alert_thresholds"] if summary["budget_ratio"] >= t]


def budget_pause_due(policy, summary, approved_cost=None):
    """True when the next AI call must wait for the user (opt-in pause at budget).

    The first pause comes when the known estimate reaches the budget; a resume approves
    the cost so far, and the next pause comes one more budget later. Calls without a price
    stay unknown and do not count.
    """
    if policy is None or not policy.get("pause_at_budget") or not policy.get("estimated_budget"):
        return False
    known = summary.get("known_cost") or 0
    return known >= (approved_cost or 0) + policy["estimated_budget"]
