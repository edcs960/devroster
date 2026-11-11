"""Project-wide AI usage, estimated cost and task progress from recorded calls.

Pure aggregation: callers pass tasks, RunnerUsage payloads and each task's approved
usage_policy. Costs reuse call_cost, so they are the same user-entered price-book
estimates the per-task view shows, never real bills. A session member that shared
another node's AI call (ai_call false) is not a call and is skipped.

Studio planning calls are not tasks. They arrive with studio_id instead of task_id
and are linked to the tasks that a handoff of that Studio project created, so a
feature's cost covers planning as well as development.
"""

from agent_dev.domain.usage_policy import call_cost

TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_input_tokens")
# Task states grouped by what the user should do about them.
STATE_GROUPS = {
    "ready": "in_progress",
    "running": "in_progress",
    "verifying": "in_progress",
    "waiting_for_capacity": "in_progress",
    "approval_pending": "needs_user",
    "user_review": "needs_user",
    "feedback_required": "needs_user",
    "pr_ready": "needs_user",
    "blocked": "blocked",
    "planned": "planned",
    "completed": "completed",
    "cancelled": "cancelled",
}
GROUPS = ("in_progress", "needs_user", "blocked", "planned", "completed", "cancelled")
# Order for the open-task list: what needs attention first.
ATTENTION = {"blocked": 0, "needs_user": 1, "in_progress": 2, "planned": 3}
OPEN_LIMIT = 50


def _bucket():
    return {"calls": 0, **dict.fromkeys(TOKEN_KEYS, 0), "total_tokens": 0, "cost": {}}


def _add(bucket, usage, cost, currency):
    bucket["calls"] += 1
    for key in TOKEN_KEYS:
        bucket[key] += usage.get(key, 0)
    bucket["total_tokens"] += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    if cost is not None:
        bucket["cost"][currency] = bucket["cost"].get(currency, 0.0) + cost


def _rounded(bucket):
    return dict(bucket, cost={k: round(v, 6) for k, v in sorted(bucket["cost"].items())})


def _usage(value):
    if not isinstance(value, dict):
        return {}
    return {
        k: v for k, v in value.items() if k in TOKEN_KEYS and type(v) is int and 0 <= v <= 10**12
    }


def progress(tasks):
    """Current task states; independent of the reporting period."""
    states: dict[str, int] = {}
    groups: dict[str, int] = dict.fromkeys(GROUPS, 0)
    for task in tasks:
        states[task["state"]] = states.get(task["state"], 0) + 1
        groups[STATE_GROUPS.get(task["state"], "planned")] += 1
    countable = len(tasks) - groups["cancelled"]
    open_tasks = sorted(
        (
            {
                "id": t["id"],
                "title": t["title"],
                "state": t["state"],
                "group": STATE_GROUPS.get(t["state"], "planned"),
                "updated_at": t.get("updated_at"),
            }
            for t in tasks
            if STATE_GROUPS.get(t["state"]) not in {"completed", "cancelled"}
        ),
        key=lambda t: (ATTENTION[t["group"]], -(t["updated_at"] or 0)),
    )
    return {
        "total": len(tasks),
        "groups": groups,
        "states": dict(sorted(states.items())),
        "completion_rate": round(groups["completed"] / countable, 4) if countable else None,
        "open": open_tasks[:OPEN_LIMIT],
        "open_total": len(open_tasks),
    }


def report(tasks, calls, policies, *, since=None, day=None, days=(), studios=None):
    """Usage and cost for calls at or after ``since`` plus current progress.

    calls: dicts with task_id (or studio_id for planning calls), payload (a RunnerUsage
    shaped payload) and created_at; policy_key picks the price book when present.
    policies: task id (or policy_key) -> normalized usage_policy or None.
    day: created_at -> "YYYY-MM-DD" label; days: the labels the daily series covers.
    studios: Studio project id -> {"title", "tasks": [task ids its handoffs created]}.
    """
    titles = {t["id"]: t for t in tasks}
    studios = studios or {}
    totals = dict(_bucket(), runner_seconds=0.0, unmeasured_calls=0, task_calls=0, studio_calls=0)
    models, roles, per_task, per_studio = {}, {}, {}, {}
    daily = {label: _bucket() for label in days}
    known_calls, unknown_calls, reasons, unpriced = 0, 0, {}, set()
    seen = set()
    for call in calls:
        payload = call["payload"] if isinstance(call.get("payload"), dict) else {}
        created = call.get("created_at") or 0
        if payload.get("ai_call") is False or (since is not None and created < since):
            continue
        attempt = payload.get("attempt_id")
        if attempt:
            if attempt in seen:
                continue
            seen.add(attempt)
        task_id, studio_id = call.get("task_id") or "", call.get("studio_id") or ""
        usage = _usage(payload.get("usage"))
        selection = payload["selection"] if isinstance(payload.get("selection"), dict) else {}
        provider = payload.get("provider") or ""
        model = selection.get("model") or ""
        key = call.get("policy_key") or task_id
        policy = policies.get(key)
        cost, currency = None, None
        if isinstance(call.get("priced"), dict):
            # Priced when recorded (Studio jobs freeze the price book at queue time).
            priced = call["priced"]
            cost, currency = priced.get("cost"), priced.get("currency")
            if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0 or not currency:
                cost, reason = None, str(priced.get("reason") or "price_missing")
                if reason == "price_book_missing":
                    unpriced.add(key)
                unknown_calls += 1
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                known_calls += 1
        elif policy is None:
            unpriced.add(key)
            unknown_calls += 1
            reasons["price_book_missing"] = reasons.get("price_book_missing", 0) + 1
        else:
            cost, reason = call_cost(
                policy, {"provider": provider, "model": model, "usage": usage, "created_at": created}
            )
            currency = policy["currency"]
            if cost is None:
                unknown_calls += 1
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                known_calls += 1
        seconds = payload.get("runner_seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and 0 <= seconds < 10**7:
            totals["runner_seconds"] += seconds
        if "input_tokens" not in usage or "output_tokens" not in usage:
            totals["unmeasured_calls"] += 1
        _add(totals, usage, cost, currency)
        _add(models.setdefault((provider, model), _bucket()), usage, cost, currency)
        _add(roles.setdefault(payload.get("phase_id") or "", _bucket()), usage, cost, currency)
        if studio_id:
            totals["studio_calls"] += 1
            _add(per_studio.setdefault(studio_id, _bucket()), usage, cost, currency)
        else:
            totals["task_calls"] += 1
            row = per_task.setdefault(task_id, dict(_bucket(), last_call_at=0))
            _add(row, usage, cost, currency)
            row["last_call_at"] = max(row["last_call_at"], created)
        label = day(created) if day else None
        if label in daily:
            _add(daily[label], usage, cost, currency)
    def by_cost(row):
        return (-sum(row[1]["cost"].values()), -row[1]["total_tokens"], str(row[0]))

    totals["runner_seconds"] = round(totals["runner_seconds"], 3)
    return {
        "progress": progress(tasks),
        "totals": _rounded(totals),
        "cost": {
            "known_calls": known_calls,
            "unknown_calls": unknown_calls,
            "reasons": dict(sorted(reasons.items())),
            "unpriced_tasks": len(unpriced),
            "note": "작업마다 승인된 가격표로 계산한 추정치입니다. 실제 청구액이 아닙니다.",
        },
        "by_model": [
            dict(_rounded(b), provider=p, model=m) for (p, m), b in sorted(models.items(), key=by_cost)
        ],
        "by_role": [dict(_rounded(b), role=r) for r, b in sorted(roles.items(), key=by_cost)],
        "by_task": [
            dict(
                _rounded(b),
                id=t,
                title=titles.get(t, {}).get("title", t),
                state=titles.get(t, {}).get("state", ""),
            )
            for t, b in sorted(per_task.items(), key=lambda r: -r[1]["last_call_at"])
        ],
        "daily": [dict(_rounded(daily[label]), day=label) for label in days],
        "by_studio": _features(studios, per_studio, per_task, titles),
    }


def _merge(*buckets):
    merged = _bucket()
    for bucket in buckets:
        for key in ("calls", *TOKEN_KEYS, "total_tokens"):
            merged[key] += bucket[key]
        for currency, amount in bucket["cost"].items():
            merged["cost"][currency] = merged["cost"].get(currency, 0.0) + amount
    return merged


def _features(studios, per_studio, per_task, titles):
    """Each Studio plan with its own generation usage and its handed-off tasks' execution."""
    rows = []
    for studio_id in sorted(set(studios) | set(per_studio)):
        info = studios.get(studio_id, {})
        linked = [t for t in info.get("tasks", []) if t in titles]
        planning = per_studio.get(studio_id, _bucket())
        execution = _merge(*(per_task[t] for t in linked if t in per_task))
        if not planning["calls"] and not execution["calls"]:
            continue
        rows.append(
            {
                "id": studio_id,
                "title": info.get("title") or studio_id,
                "tasks": [
                    {"id": t, "title": titles[t]["title"], "state": titles[t]["state"]} for t in linked
                ],
                "planning": _rounded(planning),
                "execution": _rounded(execution),
                "total": _rounded(_merge(planning, execution)),
            }
        )
    return sorted(rows, key=lambda r: (-sum(r["total"]["cost"].values()), -r["total"]["total_tokens"], r["id"]))
