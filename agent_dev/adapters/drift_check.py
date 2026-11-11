"""Run a drift check for one task: its approved requirements against its scoped diff.

The reviewer is one fresh AI call through the same runners Studio uses, under a lease in
the canonical DB, preferably on another model family than the one that wrote the code.
Results are task events (DriftStarted / DriftChecked / DriftFailed, then
DriftFindingProcessed per decision), so nothing in the canonical schema changes and the
history stays append-only. The call's tokens are a RunnerUsage event (phase "drift").
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from agent_dev.adapters.development_review import ReviewContext, collect
from agent_dev.adapters.state.sqlite.store import identifier
from agent_dev.domain import drift
from agent_dev.domain.errors import DomainError, ErrorCode


def requirements(db, task_id):
    """The imported requirements this task implements, with numbered acceptance criteria."""
    rows = db.rows(
        "SELECT DISTINCT ir.requirement_id,ir.content_json FROM requirement_links rl "
        "JOIN imported_requirements ir ON ir.import_id=rl.import_id AND ir.requirement_id=rl.requirement_id "
        "WHERE rl.kind='task' AND rl.task_id=? ORDER BY ir.requirement_id",
        (task_id,),
    )
    result = []
    for row in rows:
        value = json.loads(row["content_json"])
        criteria = value.get("criteria_ids") or []
        result.append(
            {
                "id": row["requirement_id"],
                "title": value.get("title", ""),
                "description": value.get("description", ""),
                "acceptance": [
                    {"id": cid, "text": text} for cid, text in zip(criteria, value.get("acceptance", []), strict=False)
                ],
            }
        )
    return result


def diff_text(context):
    """Scoped unified diffs (private files and out-of-scope paths already excluded)."""
    collected = collect(context, include_patches=True)
    parts, files, size, truncated = [], [], 0, not collected["complete"]
    for module in collected["modules"]:
        for entry in module["files"]:
            if entry.get("binary"):
                continue
            files.append(entry["path"])
            text = entry.get("_patch", b"").decode("utf-8", errors="replace")
            block = f"--- {module['id']}/{entry['path']} ({entry['status']})\n{context.public(text)}\n"
            if size + len(block) > drift.MAX_DIFF_CHARS:
                truncated = True
                block = block[: max(0, drift.MAX_DIFF_CHARS - size)]
            parts.append(block)
            size += len(block)
            if size >= drift.MAX_DIFF_CHARS:
                break
    return "".join(parts), files, truncated


def reviewer(config_root, developer):
    """Another model family than the developer when one is enabled, else the developer's own."""
    from agent_dev.adapters.project_setup import ai_defaults
    from agent_dev.adapters.studio_generation import runner_config
    from agent_dev.domain.model_routing import select_model
    from agent_dev.domain.role_policy import independent_family

    ai = ai_defaults(config_root)
    enabled = [p for p, row in ai["runners"].items() if row.get("enabled")]
    if not enabled:
        raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE, "사용 중인 AI 도구가 없습니다. AI 도구를 먼저 설정해주세요.")
    others = [p for p in enabled if developer and independent_family(p) != independent_family(developer)]
    provider = (others or [ai["provider"] if ai["provider"] in enabled else enabled[0]])[0]
    config = runner_config(config_root, ai, provider)
    selection = select_model(config, {"title": "Drift check", "scope": []}, "drift")
    config.update(model=selection["model"], effort=selection["effort"], routing={"mode": "fixed"})
    return provider, selection["model"], config


def _events(db, task_id, kinds):
    return [
        dict(json.loads(row["payload_json"]), _kind=row["kind"], _at=row["created_at"])
        for row in db.rows(
            "SELECT kind,payload_json,created_at FROM events WHERE aggregate_id=? AND kind IN ("
            + ",".join("?" * len(kinds))
            + ") ORDER BY version",
            (task_id, *kinds),
        )
    ]


def status(db, task_id):
    rows = _events(db, task_id, ("DriftStarted", "DriftChecked", "DriftFailed", "DriftFindingProcessed"))
    finished = {r["id"] for r in rows if r["_kind"] in {"DriftChecked", "DriftFailed"}}
    running = next((r for r in reversed(rows) if r["_kind"] == "DriftStarted" and r["id"] not in finished), None)
    failure = next((r for r in reversed(rows) if r["_kind"] == "DriftFailed"), None)
    checks = [r for r in rows if r["_kind"] == "DriftChecked"]
    decisions = [r for r in rows if r["_kind"] == "DriftFindingProcessed"]
    return {
        "running": bool(running) and time.time() - running["_at"] < 1800,
        "latest": drift.view(checks, decisions),
        "failure": failure["message"] if failure and (not checks or failure["_at"] > checks[-1]["_at"]) else "",
        "categories": drift.CATEGORIES,
    }


def start(config_root, db, task_id, secrets=(), language="Korean", generator=None, wait=False):
    """Queue one drift check in the background; ``generator`` replaces the AI call in tests."""
    from agent_dev.adapters import workspace_trust

    workspace_trust.require(config_root, "드리프트 검사")
    context = ReviewContext(db, task_id, secrets)
    if status(db, task_id)["running"]:
        raise DomainError(ErrorCode.POLICY_VIOLATION, "드리프트 검사가 이미 진행 중입니다.")
    reqs = requirements(db, task_id)
    if not reqs:
        raise DomainError(
            ErrorCode.INVALID_INPUT,
            "이 작업에 연결된 요구사항이 없습니다. Studio에서 개발 인계한 작업만 드리프트를 검사할 수 있습니다.",
        )
    diff, files, truncated = diff_text(context)
    if not files:
        raise DomainError(ErrorCode.INVALID_INPUT, "검사할 코드 변경이 없습니다.")
    usage = db.rows(
        "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='RunnerUsage' ORDER BY version DESC LIMIT 1",
        (task_id,),
    )
    developer = json.loads(usage[0]["payload_json"]).get("provider", "") if usage else ""
    provider, model, config = reviewer(config_root, developer)
    check_id = "DRIFT-" + identifier()
    context.store.event(
        task_id,
        "DriftStarted",
        {"id": check_id, "provider": provider, "model": model, "files": len(files), "truncated": truncated},
    )
    data = {"requirements": reqs, "diff": diff, "language": language}
    criteria = {c["id"] for r in reqs for c in r["acceptance"]}
    worker = threading.Thread(
        target=_run,
        args=(context, task_id, check_id, data, provider, model, config, {r["id"] for r in reqs}, criteria, files, truncated, generator),
        name="devroster-drift",
        daemon=True,
    )
    worker.start()
    if wait:
        worker.join()
    return status(db, task_id)


def _run(context, task_id, check_id, data, provider, model, config, req_ids, criteria, files, truncated, generator):
    from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
    from agent_dev.adapters.studio_generation import cli_generate
    from agent_dev.domain.execution_limits import safe_usage

    store, reports = context.store, []
    started = time.monotonic()

    def report(used_provider, used_model, usage):
        reports.append((used_provider, used_model, usage))

    try:
        workspace = Path(tempfile.mkdtemp(prefix="devroster-drift-"))
        with ExecutionSafety(store).lease("drift:" + task_id, ttl=120) as lease:
            grant = dict(lease, db=str(context.db.path), runner_config=config, usage_sink=report)
            output = (generator or cli_generate)("drift", data, provider, model, workspace, grant)
        findings = drift.parse(output, req_ids, criteria, set(files))
        store.event(
            task_id,
            "DriftChecked",
            {"id": check_id, "provider": provider, "model": model, "files": len(files), "truncated": truncated, "findings": findings},
        )
    except Exception as exc:  # noqa: BLE001 -- the screen shows a safe reason, never provider output.
        message = str(exc) if isinstance(exc, DomainError) else "드리프트 검사에 실패했습니다. AI 도구 로그인·키를 확인해주세요."
        store.event(task_id, "DriftFailed", {"id": check_id, "message": message[:500], "cause": type(exc).__name__})
    finally:
        seconds = round(time.monotonic() - started, 3)
        for used_provider, used_model, usage in reports:
            store.event(
                task_id,
                "RunnerUsage",
                {
                    "attempt_id": check_id,
                    "phase_id": "drift",
                    "provider": used_provider,
                    "usage": safe_usage(usage),
                    "selection": {"model": used_model or model},
                    "runner_seconds": seconds,
                    "ai_call": True,
                },
            )


def decide(db, task_id, check_id, finding_id, state, note=""):
    if state not in drift.STATES:
        raise DomainError(ErrorCode.INVALID_INPUT, "처리 상태를 확인해주세요.")
    note = note.strip()[:4000] if isinstance(note, str) else ""
    if state in {"accepted", "rebutted"} and not note:
        raise DomainError(ErrorCode.INVALID_INPUT, "수용·반박에는 이유를 적어주세요.")
    latest = status(db, task_id)["latest"]
    if not latest or latest["id"] != check_id or finding_id not in {f["id"] for f in latest["findings"]}:
        raise DomainError(ErrorCode.STALE_VERSION, "최신 드리프트 검사의 지적이 아닙니다. 새로 불러와 주세요.")
    ReviewContext(db, task_id).store.event(
        task_id, "DriftFindingProcessed", {"check_id": check_id, "finding_id": finding_id, "state": state, "note": note}
    )
    return status(db, task_id)
