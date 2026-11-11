"""Human-facing guidance; all decisions and permissions stay in the workflow."""

import re
import shlex

from agent_dev.domain.status import STATE_TEXT as STATES  # Shared with the Slack listener.

PHASES = {
    "planner": "요구사항 정리",
    "architect": "구조 설계",
    "dba": "데이터 설계",
    "ux_ui": "사용 흐름 설계",
    "ux-ui": "사용 흐름 설계",
    "designer": "화면 디자인",
    "fmea_expert": "FMEA 방법론 검토",
    "fmea-expert": "FMEA 방법론 검토",
    "developer_common": "구현 준비",
    "developer-common": "구현 준비",
    "developer": "개발",
    "developer_frontend": "화면 개발",
    "developer-frontend": "화면 개발",
    "developer_backend": "서버 개발",
    "developer-backend": "서버 개발",
    "developer_infra": "실행환경 구성",
    "developer-infra": "실행환경 구성",
    "qa": "품질 확인",
    "test_automation": "자동 테스트",
    "test-automation": "자동 테스트",
    "security": "보안 검토",
    "reviewer": "최종 리뷰",
    "tech_writer": "사용 안내 작성",
    "tech-writer": "사용 안내 작성",
    "verification": "결과 검증",
}
REASONS = {
    "FEEDBACK_PENDING": (
        "수정 요청에 대한 계획이 필요합니다",
        "피드백을 검토하고 새 실행 계획을 승인한 뒤 진행하세요.",
    ),
    "DESIGN_APPROVAL_REQUIRED": ("설계 확인이 필요합니다", "현재 설계 버전을 검토하고 승인하세요."),
    "PLAN_APPROVAL_REQUIRED": (
        "실행 계획 승인이 필요합니다",
        "작업 범위와 수용 기준을 확인한 뒤 계획을 승인하세요.",
    ),
    "SNAPSHOT_MISSING": (
        "작업 준비가 끝나지 않았습니다",
        "계획을 승인하면 확정된 입력과 분리된 작업 폴더를 준비합니다.",
    ),
    "DEPENDENCY_PENDING": (
        "먼저 끝내야 할 작업이 있습니다",
        "아래 선행 작업을 완료한 뒤 진행하세요.",
    ),
    "EXECUTION_DISABLED": (
        "자동 실행이 꺼져 있습니다",
        "작업 공간 설정을 검토한 뒤 터미널에서 실행 허용 여부를 결정하세요.",
    ),
    "CAPACITY_WAIT": (
        "실행 가능한 AI를 기다립니다",
        "AI 사용량과 동시 실행 한도를 확인하고 대기 시간이 지난 뒤 재개하세요.",
    ),
    "OPEN_FINDINGS": (
        "검토에서 수정할 부분을 찾았습니다",
        "발견 사항의 담당 단계와 근거를 확인하고 수정하세요.",
    ),
    "VERIFICATION_FAILED": (
        "필수 검사에 실패했습니다",
        "실패한 검사의 상세 결과를 확인하고 수정 후 다시 검증하세요.",
    ),
    "SNAPSHOT_RELOCATION": (
        "다른 작업 공간의 기록입니다",
        "이 기록을 재개하지 말고 현재 프로젝트에서 새 계획을 검토·승인하세요.",
    ),
    "RUNNER_CONFIGURATION": (
        "승인된 AI 실행 설정이 준비되지 않았습니다",
        "실행 도구와 인증 설정을 확인하고 새 계획을 검토·승인하세요.",
    ),
    "TASK_BLOCKED": (
        "작업이 중단되었습니다",
        "최근 실행 기록에서 원인을 확인하고 해결한 뒤 재개하세요.",
    ),
    "VERIFICATION_PENDING": (
        "결과 검증을 실행하세요",
        "테스트와 필수 검사가 모두 통과해야 다음 단계로 넘어갑니다.",
    ),
    "REVIEW_PENDING": (
        "변경사항 확인이 남았습니다",
        "PR, CI와 게시 정책을 확인하세요. 검증 통과만으로 완료되지는 않습니다.",
    ),
    "INTERRUPTED_CHANGES": (
        "중단된 단계가 남긴 변경을 확인하세요",
        "권한 밖 변경은 복사해 두었습니다. 이어서 실행하면 그 파일들을 되돌리고 다시 실행합니다.",
    ),
    "BUDGET_PAUSE": (
        "예산에 도달해 멈췄습니다",
        "사용량을 확인하세요. 이어서 실행하면 예산만큼 더 진행한 뒤 다시 멈춥니다.",
    ),
    "EXECUTION_LIMIT_REACHED": (
        "승인된 실행 한도에 도달했습니다",
        "사용량을 확인하세요. 재개해도 한도가 초기화되지 않습니다. 추가 작업은 새 계획이 필요합니다.",
    ),
    "LEGACY_RECORD": (
        "기존 방식으로 관리되는 작업입니다",
        "제목·범위·완료 기준을 확인할 수 있습니다. 세부 실행 기록은 기존 작업 화면과 체크포인트에서 확인하세요.",
    ),
    "INVALID_INPUT": (
        "AI 응답 또는 입력 형식을 확인하세요",
        "최근 실행의 원문을 확인하고 역할 지시와 입력 형식을 수정하세요.",
    ),
    "AUTH_REQUIRED": (
        "AI 로그인 확인이 필요합니다",
        "선택한 실행 도구에서 로그인 상태를 확인하세요.",
    ),
    "CAPABILITY_UNAVAILABLE": (
        "필요한 실행 도구를 사용할 수 없습니다",
        "도구 설치와 선택한 모델의 지원 여부를 확인하세요.",
    ),
    "CAPACITY_EXHAUSTED": (
        "실행 한도 또는 사용량이 부족합니다",
        "승인된 한도와 AI 제공자의 사용량을 확인하세요.",
    ),
    "POLICY_VIOLATION": (
        "허용된 작업 범위를 확인하세요",
        "작업 경로와 승인된 정책을 검토하세요. 자동으로 권한을 넓히지 않습니다.",
    ),
    "LEASE_CONFLICT": (
        "다른 작업이 같은 자원을 사용 중입니다",
        "진행 중인 작업이 끝난 뒤 다시 확인하세요.",
    ),
    "EXTERNAL_TRANSIENT": (
        "외부 서비스에 연결하지 못했습니다",
        "서비스 상태와 연결을 확인하고 잠시 뒤 다시 시도하세요.",
    ),
}


def phase_label(value):
    key = re.sub(r"^\d+-", "", value)
    return PHASES.get(key, value)


def task_display(data):
    task = data["task"]
    state = task["state"]
    title, description = STATES.get(
        state, ("상태 확인 필요", "원문 상태와 작업 기록을 확인하세요.")
    )
    reasons = [
        dict(
            row,
            title=REASONS.get(row["code"], ("추가 확인이 필요합니다", "상세 기록을 확인하세요."))[
                0
            ],
            guidance=REASONS.get(row["code"], ("", "상세 기록을 확인하세요."))[1],
        )
        for row in data["reasons"]
    ]
    latest = {}
    for attempt in data["attempts"]:
        latest.setdefault(attempt["phase_id"], attempt)
    phases = []
    nodes = data.get("node_states") or {}
    for value in (data.get("snapshot") or {}).get("phases", []):
        attempt = latest.get(value)
        phases.append(
            {
                "id": value,
                "label": phase_label(value),
                # A role its condition skipped is not waiting.
                "state": "skipped"
                if nodes.get(value) == "skipped"
                else attempt["state"]
                if attempt
                else "passed"
                if nodes.get(value) == "passed"
                else "queued",
            }
        )
    counted = [row for row in phases if row["state"] != "skipped"]
    passed = sum(row["state"] == "passed" for row in counted)
    actions = [
        {"title": row["title"], "description": row["guidance"], "command": ""} for row in reasons
    ]
    # Inspection commands are always safe to suggest. Mutating CLI actions are
    # suggestions only; the workflow still enforces all approvals and limits.
    command = ""
    if data.get("source") == "legacy":
        command = shlex.join(["devroster", "resume", task["task_id"]])
    elif not reasons and state == "ready":
        command = shlex.join(["devroster", "task", "run", task["task_id"]])
    elif state == "verifying" and all(row["code"] == "VERIFICATION_PENDING" for row in reasons):
        command = shlex.join(["devroster", "task", "verify", task["task_id"]])
    else:
        command = shlex.join(["devroster", "task", "explain", task["task_id"]])
    actions.append(
        {
            "title": "기록·재개 안내 확인"
            if data.get("source") == "legacy"
            else "작업 실행"
            if not reasons and state == "ready"
            else "결과 검증"
            if " verify " in command
            else "터미널에서 상세 확인",
            "description": "프로젝트 폴더의 터미널에서 실행하세요. 아래 버튼은 명령을 복사합니다.",
            "command": command,
        }
    )
    return {
        "state_label": title,
        "summary": description,
        "reasons": reasons,
        "actions": actions,
        "phases": phases,
        "completed_phases": passed,
        "total_phases": len(counted),
        "progress_percent": round(passed * 100 / len(counted)) if counted else None,
        "source_label": "기존 작업 기록" if data.get("source") == "legacy" else "승인 기반 작업",
    }
