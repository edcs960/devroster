"""Automatic price list: published per-token prices of API models, with their history.

The project's own price book always wins; this list only prices an API model the owner
did not enter. It is read from a public community list (LiteLLM's model price file), holds
USD base rates only (no long-context, batch or cache-write surcharge) and is an
approximation, never a bill. A subscription CLI is not priced from it: its calls are not
billed per token. Pure: conversion, history and lookup.
"""

import math
from datetime import UTC, date, datetime

SOURCE_NAME = "LiteLLM"
SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
CURRENCY = "USD"
# DevRoster API provider -> vendor name in the source list.
VENDORS = {
    "openai_api": "openai",
    "anthropic_api": "anthropic",
    "gemini_api": "gemini",
    "xai_api": "xai",
}
MODES = {"chat", "responses"}
RATE_KEYS = ("input_per_million", "cached_input_per_million", "output_per_million")
SOURCE_RATES = {
    "input_per_million": "input_cost_per_token",
    "cached_input_per_million": "cache_read_input_token_cost",
    "output_per_million": "output_cost_per_token",
}
EPOCH = "1970-01-01"
MAX_MODELS = 2000
# Prices kept per model: the current one and the changes before it.
HISTORY = 5
# A per-token price above this (10,000 per million tokens) is a broken entry, not a price.
MAX_TOKEN_PRICE = 0.01


def _per_million(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0 <= value <= MAX_TOKEN_PRICE:
        return None
    return round(value * 1_000_000, 6)


def _rate(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )


def _model_name(value):
    return isinstance(value, str) and 1 <= len(value) <= 120 and value.isprintable()


def from_source(data):
    """Rows {"vendor", "model", rates per million tokens} for the vendors DevRoster calls.

    An entry without a usable input and output price is left out; a missing cached-input
    price stays None (unknown), never 0.
    """
    if not isinstance(data, dict):
        return []
    vendors = set(VENDORS.values())
    rows = {}
    for key, entry in data.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        vendor = entry.get("litellm_provider")
        if vendor not in vendors or entry.get("mode") not in MODES:
            continue
        prefixed = key.startswith(vendor + "/")
        model = key.removeprefix(vendor + "/")
        if not _model_name(model) or "/" in model or model.startswith("ft:"):
            continue
        if (vendor, model) in rows and not prefixed:
            continue
        rates = {name: _per_million(entry.get(source)) for name, source in SOURCE_RATES.items()}
        if rates["input_per_million"] is None or rates["output_per_million"] is None:
            continue
        rows[(vendor, model)] = {"vendor": vendor, "model": model, **rates}
    return [rows[key] for key in sorted(rows)][:MAX_MODELS]


def valid_rows(value):
    """The well-formed history rows of a stored list; anything else is dropped."""
    rows = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict) or row.get("vendor") not in set(VENDORS.values()):
            continue
        day = row.get("valid_from")
        if not _model_name(row.get("model")) or not isinstance(day, str) or len(day) != 10:
            continue
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        rates = {name: row.get(name) for name in RATE_KEYS}
        cached = rates["cached_input_per_million"]
        if not _rate(rates["input_per_million"]) or not _rate(rates["output_per_million"]):
            continue
        if cached is not None and not _rate(cached):
            continue
        rows.append(
            {"vendor": row["vendor"], "model": row["model"], "valid_from": row["valid_from"], **rates}
        )
    return sorted(rows, key=lambda r: (r["vendor"], r["model"], r["valid_from"]))


def merge(previous, fresh, today):
    """``previous`` history plus today's prices: a changed price starts a new row from today.

    A model seen for the first time is priced from the beginning, since no earlier price is
    known. A model the source dropped keeps its rows, so its past calls stay priced.
    """
    history = {}
    for row in valid_rows(previous):
        history.setdefault((row["vendor"], row["model"]), []).append(row)
    for row in fresh:
        rows = history.setdefault((row["vendor"], row["model"]), [])
        if not rows:
            rows.append(dict(row, valid_from=EPOCH))
        elif any(rows[-1][name] != row[name] for name in RATE_KEYS):
            if rows[-1]["valid_from"] >= today:
                rows[-1] = dict(row, valid_from=rows[-1]["valid_from"])
            else:
                rows.append(dict(row, valid_from=today))
    return [row for key in sorted(history) for row in history[key][-HISTORY:]]


def price(rows, provider, model, when):
    """The row that prices ``model`` of an API provider at ``when``; None when not listed."""
    vendor = VENDORS.get(provider)
    if vendor is None or not isinstance(model, str):
        return None
    name = model.removeprefix("models/") if vendor == "gemini" else model
    day = (
        datetime.fromtimestamp(when, UTC).date().isoformat()
        if isinstance(when, (int, float))
        else None
    )
    candidates = [
        row
        for row in rows
        if row["vendor"] == vendor
        and row["model"] == name
        and (day is None or row["valid_from"] <= day)
    ]
    return max(candidates, key=lambda row: row["valid_from"], default=None)


def models(rows):
    """Number of distinct models the list prices."""
    return len({(row["vendor"], row["model"]) for row in rows})
