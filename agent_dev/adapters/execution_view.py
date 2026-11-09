"""Execution and cost view model (EX-01/CO-01) over existing canonical records.

Reads tasks, attempts, workflow node runs, events, leases, verification and
publish rows; nothing is written on GET and no new timeline store exists.
Event payloads are reduced to known safe fields, so outputs and any private
reasoning never reach the screen.
"""

from __future__ import annotations

import json
import time

from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.usage_policy import estimate, normalize_policy

TERMINAL = {"completed", "cancelled"}
EVENT_LABELS = {
    "TaskApproved": "작업 승인·실행 준비",
    "TaskStateChanged": "작업 상태 변경",
    "PhaseStarted": "단계 시작",
    "PhaseFinished": "단계 종료",
    "RunnerUsage": "AI 실행 기록",
    "RunnerSelected": "AI 도구·모델 선택",
    "ExecutionBlocked": "실행 차단",
    "VerificationFinished": "검증 완료",
    "VerificationStarted": "검증 시작",
    "CancelRequested": "취소 요청",
    "TaskCancelled": "작업 취소",
    "FindingDetected": "검토 지적",
    "UsageThresholdReached": "예상 비용 알림",
    "ReviewDecisionRecorded": "검토 결정",
    "PreviewStarted": "미리보기 연결",
    "PreviewCaptured": "화면 캡처",
    "PublishRecorded": "게시 기록",
    "HookRun": "프로젝트 훅 실행",
}
SAFE_FIELDS = {
    "phase_id", "state", "from", "to", "code", "reason", "provider", "attempt_id",
    "gate", "passed", "decision", "kind", "threshold", "session_id", "generation",
    "runner_seconds", "node_id",
}
STATE_LABELS = {
    "planned": "계획됨", "approval_pending": "승인 대기", "ready": "실행 준비", "running": "실행 중",
    "blocked": "차단됨", "waiting_for_capacity": "AI 사용량 대기", "verifying": "검증 중",
    "pr_ready": "게시 준비", "user_review": "사용자 검토", "feedback_required": "피드백 필요",
    "completed": "완료", "cancelled": "취소됨",
}

# Screen wording for explain() reason codes; the CLI keeps the original English text.
NEXT_ACTIONS = {
    "DESIGN_APPROVAL_REQUIRED": ("현재 설계 승인이 없거나 무효가 되었습니다.", "최신 설계 버전을 검토하고 승인하세요."),
    "PLAN_APPROVAL_REQUIRED": ("계획 승인이 없거나 무효가 되었습니다.", "계획을 검토하고 승인하세요. (devroster plan approve PLAN-ID)"),
    "SNAPSHOT_MISSING": ("작업 실행 조건(스냅샷)이 아직 없습니다.", "연결된 계획을 승인하면 실행 조건과 작업 폴더가 준비됩니다."),
    "DEPENDENCY_PENDING": ("선행 작업이 끝나지 않았습니다.", "선행 작업을 먼저 완료하세요."),
    "EXECUTION_DISABLED": ("이 작업 공간은 승인 기반 실행이 꺼져 있습니다(기존 자료를 옮겨 온 작업 공간).", "이전 비교 결과를 확인한 뒤 터미널에서 실행을 켜세요: devroster refactor configure execute"),
    "CAPACITY_WAIT": ("AI 도구 사용량 한도로 대기 중입니다.", "한도가 풀린 뒤 이어서 실행하세요. (devroster task resume {task})"),
    "OPEN_FINDINGS": ("검토 지적 사항이 남아 있습니다.", "담당 단계와 아래 근거를 확인한 뒤 이어서 실행하세요."),
    "VERIFICATION_FAILED": ("최근 필수 검증이 통과하지 않았습니다.", "검증 근거를 확인하고 수정한 뒤 다시 검증하세요."),
    "SNAPSHOT_RELOCATION": ("다른 작업 공간의 폴더를 가리키는 과거 실행 조건입니다.", "대상 저장소 설정을 확인하고 새 계획·작업을 승인하세요. 과거 작업은 이어서 실행하지 않습니다."),
    "RUNNER_CONFIGURATION": ("승인된 실행 조건에 사용 가능한(로그인된) AI 도구가 없습니다.", "AI 도구 설정을 확인하고 작업을 새로 승인하세요. 승인된 실행 조건은 바뀌지 않습니다."),
    "WORKFLOW_APPROVAL_PENDING": ("단계 승인을 기다리고 있습니다. 오류가 아닙니다.", "개발 흐름 화면에서 결과를 확인하고 승인·반려한 뒤 이어서 실행하세요. (/workflows?task={task})"),
    "TASK_BLOCKED": ("작업이 차단되었습니다.", "최근 기록에서 원인을 해결한 뒤 이어서 실행하세요. (devroster task resume {task})"),
    "VERIFICATION_PENDING": ("단계 실행이 끝났고 완료 확인(검증)이 남았습니다.", "검증을 실행하세요. (devroster task verify {task})"),
    "REVIEW_PENDING": ("검증을 통과했습니다. 게시와 완료 처리가 남았습니다.", "변경 검토 후 게시하고 완료하세요. 로컬 완료면 완료할 때 기본 브랜치에 병합되고, GitHub 방식이면 PR·CI 통과가 필요합니다."),
    "FEEDBACK_PENDING": ("사용자 피드백을 반영할 수정 계획 승인이 필요합니다.", "피드백을 검토하고 새 계획을 승인하세요."),
    "BUDGET_PAUSE": ("예상 비용이 설정한 예산에 도달해 다음 AI 호출 전에 멈췄습니다.", "사용량을 확인한 뒤 계속하려면 이어서 실행하세요. 이어서 실행하면 예산만큼 더 진행하고 다시 멈춥니다. (devroster task resume {task})"),
    "EXECUTION_LIMIT_REACHED": ("승인된 실행 횟수 또는 AI 실행 시간을 모두 사용했습니다.", "사용량과 지적 사항을 확인하세요. 추가 범위는 새 작업으로 승인해야 하며, 이어서 실행해도 한도는 초기화되지 않습니다."),
    "LEGACY_RECORD": ("기존 방식으로 관리되는 작업입니다.", "기존 체크포인트와 기존 명령으로 진행하세요."),
}


def next_actions(reasons, task_id):
    rows = []
    for row in reasons:
        known = NEXT_ACTIONS.get(row.get("code", ""))
        if not known:
            rows.append(row)
            continue
        message, action = known
        if row["code"] == "DEPENDENCY_PENDING" and ":" in row.get("message", ""):
            message += " " + row["message"].split(":", 1)[1].strip()
        rows.append({"code": row["code"], "message": message, "action": action.format(task=task_id)})
    return rows


def _safe(payload, secrets):
    values: dict = {k: v for k, v in payload.items() if k in SAFE_FIELDS and isinstance(v, (str, int, float, bool))}
    selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
    for key in ("model", "effort", "reason", "mode"):
        if isinstance(selection.get(key), str):
            values["selection_" + key] = selection[key][:200]
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    if usage is not None:
        values["usage"] = {k: v for k, v in usage.items() if isinstance(v, int)}
    return redact(values, secrets)


def usage_calls(db, task_id):
    """Recorded canonical calls; each attempt is one call (call_id = attempt id)."""
    rows = db.rows(
        "SELECT payload_json,created_at FROM events WHERE aggregate_id=? AND kind='RunnerUsage' ORDER BY version",
        (task_id,),
    )
    calls = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
        calls.append(
            {
                "call_id": payload.get("attempt_id"),
                "provider": payload.get("provider") or "",
                "model": selection.get("model") or "",
                "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
                "created_at": row["created_at"],
                "source": "canonical",
            }
        )
    return calls


def task_policy(repository, task_id):
    try:
        resolved = repository.snapshot(task_id)["resolved"]
    except DomainError:
        return None
    return normalize_policy(resolved.get("usage_policy"))


def cost(db, task_id):
    repository = WorkflowRepository(db)
    policy = task_policy(repository, task_id)
    summary = estimate(policy, usage_calls(db, task_id))
    summary["studio_calls"] = "Studio 생성 사용량은 작업과 연결된 기록이 없어 합산하지 않습니다."
    return summary


def _stages(db, task, resolved):
    graph = resolved.get("workflow_graph") or {}
    # Work the user waits on: roles, approvals, verification and tools (not routing/merge).
    work_nodes = [
        n["id"] for n in graph.get("nodes", []) if n.get("kind") in {"role", "approval", "verification", "tool"}
    ]
    phases = work_nodes or [p["id"] if isinstance(p, dict) else p for p in resolved.get("phases", [])]
    runs = {
        row["node_id"]: row["state"]
        for row in db.rows(
            "SELECT node_id,state FROM workflow_node_runs WHERE task_id=? AND generation=? ",
            (task["id"], task["generation"]),
        )
    }
    attempts = db.rows(
        "SELECT phase_id,state FROM phase_attempts WHERE task_id=? AND generation=? ORDER BY started_at,attempt_no",
        (task["id"], task["generation"]),
    )
    latest = {}
    for row in attempts:
        latest[row["phase_id"]] = row["state"]
    counts = {"total": len(phases), "passed": 0, "running": 0, "failed": 0, "skipped": 0, "awaiting_approval": 0, "not_started": 0}
    sessions = {n["id"]: n["session"] for n in graph.get("nodes", []) if n.get("kind") == "role" and n.get("session")}
    items = []
    for phase in phases:
        state = runs.get(phase) or latest.get(phase) or "not_started"
        bucket = {"queued": "running", "interrupted": "failed", "superseded": "failed", "rejected": "failed", "pending": "not_started", "waiting_for_capacity": "running"}.get(state, state)
        counts[bucket if bucket in counts else "not_started"] += 1
        item = {"id": phase, "state": state, "bucket": bucket if bucket in counts else "not_started"}
        if phase in sessions:
            item["session"] = sessions[phase]
        items.append(item)
    return counts, items


def _sessions(items):
    """Session progress in execution order (ADR 0002): one row per session span."""
    rows = {}
    for item in items:
        if item.get("session"):
            rows.setdefault(item["session"], []).append(item["bucket"])
    result = []
    for session, buckets in rows.items():
        if "failed" in buckets:
            state = "failed"
        elif all(b == "skipped" for b in buckets):
            state = "skipped"
        elif all(b in {"passed", "skipped"} for b in buckets):
            state = "passed"
        elif any(b in {"running", "passed"} for b in buckets):
            state = "running"
        else:
            state = "not_started"
        result.append({"id": session, "state": state, "roles": len(buckets)})
    return result


def view(root, task_id, cursor=0, now=None):
    from agent_dev.adapters.task_queries import TaskQueries

    queries = TaskQueries(root)
    if not queries.db:
        raise DomainError(ErrorCode.INVALID_INPUT, "프로젝트 작업 공간을 먼저 준비해주세요.")
    if type(cursor) is not int or cursor < 0:
        raise DomainError(ErrorCode.INVALID_INPUT, "cursor를 확인해주세요.")
    db, secrets = queries.db, queries.secrets
    explained = queries.explain(task_id)
    task = db.rows("SELECT * FROM tasks WHERE id=?", (task_id,))[0]
    repository = WorkflowRepository(db)
    try:
        resolved = repository.snapshot(task_id)["resolved"] if task["snapshot_id"] else {}
    except DomainError:
        resolved = {}
    now = time.time() if now is None else now
    counts, stages = _stages(db, task, resolved)
    events = db.rows(
        "SELECT version,kind,payload_json,created_at FROM events WHERE aggregate_id=? AND version>? ORDER BY version LIMIT 100",
        (task_id, cursor),
    )
    timeline = [
        {
            "version": row["version"],
            "kind": row["kind"],
            "label": EVENT_LABELS.get(row["kind"], row["kind"]),
            "at": row["created_at"],
            "details": _safe(json.loads(row["payload_json"]), secrets),
        }
        for row in events
    ]
    last = db.rows("SELECT COALESCE(MAX(version),0) AS v FROM events WHERE aggregate_id=?", (task_id,))[0]["v"]
    # Current selection comes from the latest record, not the cursor-filtered page.
    latest_usage = db.rows(
        "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='RunnerUsage' ORDER BY version DESC LIMIT 1",
        (task_id,),
    )
    current_attempt = db.rows(
        "SELECT phase_id,started_at FROM phase_attempts WHERE task_id=? AND state='running' ORDER BY started_at DESC LIMIT 1",
        (task_id,),
    )
    selected = _safe(json.loads(latest_usage[0]["payload_json"]), secrets) if latest_usage else {}
    lease = db.rows("SELECT expires_at FROM leases WHERE resource=?", ("task:" + task_id,))
    lease_expires = lease[0]["expires_at"] if lease and lease[0]["expires_at"] > now else None
    started = db.rows("SELECT MIN(started_at) AS s FROM phase_attempts WHERE task_id=?", (task_id,))[0]["s"]
    terminal = task["state"] in TERMINAL
    usage = queries.usage(task_id) if task["snapshot_id"] else {"usage": None, "limits": None}
    return {
        "task": {"id": task_id, "title": redact(task["title"], secrets), "state": task["state"], "state_label": STATE_LABELS.get(task["state"], task["state"]), "generation": task["generation"]},
        "terminal": terminal,
        "poll": {"active_ms": 0 if terminal else 3000, "hidden_ms": 0 if terminal else 30000},
        "started_at": started,
        "elapsed_seconds": round(now - started, 1) if started else None,
        "stages": counts,
        "stage_items": stages,
        "sessions": _sessions(stages),
        "current": {
            "phase": current_attempt[0]["phase_id"] if current_attempt else None,
            "provider": selected.get("provider"),
            "model": selected.get("selection_model") or None,
            "effort": selected.get("selection_effort") or None,
            "reason": selected.get("selection_reason") or None,
            "recorded": bool(selected),
        },
        "wait": {
            "capacity": task["state"] == "waiting_for_capacity",
            "lease_expires_at": lease_expires,
            "note": "실행 소유권(lease)이 만료되기 전에는 다른 실행을 시작할 수 없습니다." if lease_expires else "",
        },
        "next_actions": next_actions(explained.get("reasons", []), task_id),
        "timeline": timeline,
        "cursor": last,
        "usage": usage.get("usage"),
        "limits": usage.get("limits"),
        "cost": cost(db, task_id) if task["snapshot_id"] else None,
    }
