"""Run a drift check for one task: its approved requirements against its scoped diff.

The reviewer is one fresh AI call through the same runners Studio uses, under a lease in
the canonical DB, preferably on another model family than the one that wrote the code.
Results are task events (DriftStarted / DriftChecked / DriftFailed, then
DriftFindingProcessed per decision), so nothing in the canonical schema changes and the
history stays append-only. The call's tokens are a RunnerUsage event (phase "drift").

A project can join the check to verification (``drift.gate`` in the approved snapshot):
``after_verification`` runs it when the gates pass, and ``require_clear`` refuses a publish
until the check of the verified code has no open high finding (mode "block").
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

from agent_dev.adapters.development_review import ReviewContext, collect
from agent_dev.adapters.state.sqlite.store import identifier
from agent_dev.adapters.worktrees import evidence_digest
from agent_dev.domain import criteria_tests, drift, review_scorecard
from agent_dev.domain.content import digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode


def named_tests(db, task_id):
    """Criterion id -> test selector, as Test Automation last named them for this task."""
    rows = db.rows(
        "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='CriteriaTestsNamed' ORDER BY version DESC LIMIT 1",
        (task_id,),
    )
    named = json.loads(rows[0]["payload_json"]).get("criteria_tests") if rows else None
    return named if isinstance(named, dict) else {}


def requirements(db, task_id):
    """The imported requirements this task implements, with numbered acceptance criteria.

    Each criterion carries ``test``: the test named for it, or "".
    """
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
    return criteria_tests.attach(result, named_tests(db, task_id)) if result else result


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


def notebook(db, task_id, prefer=""):
    """What the owner already rebutted or accepted in this task's earlier checks, with the reason."""
    rows = _events(db, task_id, ("DriftChecked", "DriftFindingProcessed"))
    findings = drift.decided(
        [r for r in rows if r["_kind"] == "DriftChecked"], [r for r in rows if r["_kind"] == "DriftFindingProcessed"]
    )
    return review_scorecard.notebook(
        [
            {
                "title": f.get("title"),
                "state": f["state"],
                "note": f["note"],
                "about": f.get("criterion_id") or f.get("requirement_id") or f.get("file"),
                "group": f.get("category"),
            }
            for f in findings
        ],
        prefer=prefer,
    )


def evidence(resolved):
    """What a passed verification run stores as its policy digest, for the code as it is now."""
    commands = resolved.get("verification", {}).get("commands", [])
    return digest({"commands": commands, "source": evidence_digest(resolved.get("repositories", {}))})


def _resolved(db, task_id):
    rows = db.rows(
        "SELECT s.resolved_json FROM input_snapshots s JOIN tasks t ON t.snapshot_id=s.id WHERE t.id=?", (task_id,)
    )
    return json.loads(rows[0]["resolved_json"]) if rows else {}


def _verified(db, task_id):
    """The evidence digest of the latest verification run when it passed, else ""."""
    runs = db.rows(
        "SELECT state,policy_digest FROM verification_runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)
    )
    return runs[0]["policy_digest"] if runs and runs[0]["state"] == "passed" else ""


def status(db, task_id):
    rows = _events(db, task_id, ("DriftStarted", "DriftChecked", "DriftFailed", "DriftFindingProcessed"))
    finished = {r["id"] for r in rows if r["_kind"] in {"DriftChecked", "DriftFailed"}}
    running = next((r for r in reversed(rows) if r["_kind"] == "DriftStarted" and r["id"] not in finished), None)
    failure = next((r for r in reversed(rows) if r["_kind"] == "DriftFailed"), None)
    checks = [r for r in rows if r["_kind"] == "DriftChecked"]
    decisions = [r for r in rows if r["_kind"] == "DriftFindingProcessed"]
    result = {
        "running": bool(running) and time.time() - running["_at"] < 1800,
        "latest": drift.view(checks, decisions),
        "failure": failure["message"] if failure and (not checks or failure["_at"] > checks[-1]["_at"]) else "",
        "categories": drift.CATEGORIES,
    }
    reqs = requirements(db, task_id)
    # Deterministic, no AI: which criteria have a test named for them at all.
    result["criteria"] = criteria_tests.coverage(reqs)
    mode = drift.gate_mode(_resolved(db, task_id))
    result["gate"] = drift.gate(
        mode,
        bool(reqs),
        result["latest"],
        result["running"],
        bool(result["failure"]),
        _verified(db, task_id) if mode != "off" else "",
    )
    return result


def require_clear(db, task_id):
    """Refuse a publish while the project's drift gate blocks it (mode "block" only)."""
    gate = status(db, task_id)["gate"]
    if gate["blocks"]:
        raise DomainError(ErrorCode.VERIFICATION_FAILED, "드리프트 검사 때문에 게시할 수 없습니다. " + gate["message"])
    return gate


def after_verification(config_root, db, task_id, generator=None):
    """Run the check once verification passed, when the project joined it to verification.

    Waits for the reviewer: verification runs in a command that ends right after. Never
    raises: the check is evidence beside the gates, and a failure is recorded as DriftFailed.
    """
    try:
        gate = status(db, task_id)["gate"]
        if gate["state"] in {"off", "not_applicable", "running", "open", "clear"}:
            return gate
        from agent_dev.adapters.secrets import registered_secrets
        from agent_dev.adapters.studio_generation import project_guidance
        from agent_dev.domain.output_language import DEFAULT

        language = project_guidance(config_root, "specs").get("language") or DEFAULT
        return start(config_root, db, task_id, registered_secrets(config_root), language, generator, wait=True)["gate"]
    except Exception as exc:  # noqa: BLE001 -- verification already passed; report, never fail it.
        message = str(exc) if isinstance(exc, DomainError) else "드리프트 검사를 시작하지 못했습니다."
        return {"mode": "", "state": "skipped", "blocks": False, "message": message}


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
    # The check counts for exactly this code and these gates (see drift.gate).
    checked = evidence(context.resolved)
    context.store.event(
        task_id,
        "DriftStarted",
        {"id": check_id, "provider": provider, "model": model, "files": len(files), "truncated": truncated},
    )
    data = {"requirements": reqs, "diff": diff, "language": language}
    earlier = redact(notebook(db, task_id), context.secrets)
    if earlier:
        data["decided"] = earlier
    criteria = {c["id"] for r in reqs for c in r["acceptance"]}
    worker = threading.Thread(
        target=_run,
        args=(context, task_id, check_id, data, provider, model, config, {r["id"] for r in reqs}, criteria, files, truncated, generator, checked),
        name="devroster-drift",
        daemon=True,
    )
    worker.start()
    if wait:
        worker.join()
    return status(db, task_id)


def _run(context, task_id, check_id, data, provider, model, config, req_ids, criteria, files, truncated, generator, checked=""):
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
            {
                "id": check_id,
                "provider": provider,
                "model": model,
                "files": len(files),
                "truncated": truncated,
                "findings": findings,
                "evidence": checked,
            },
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
