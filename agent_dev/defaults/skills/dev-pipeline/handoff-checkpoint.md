# 핸드오프 체크포인트 공통 양식

Claude Code와 Codex가 작업을 주고받을 때 사용하는 공통 문서 양식이다.

- 파일 위치: `output/<주제>/<TASK-ID>/latest-checkpoint.md` (이력은 `checkpoints/<타임스탬프>-checkpoint.md`로 보관)
- 갱신 시점: 각 역할 단계 완료 시, 작업 중단 시, 에이전트 전환 직전. 사용자 요청이 없어도 작업 단위 완료·위험 작업 직전·컨텍스트 압축/사용량 한도 임박 시 에이전트가 자율 갱신한다(`AGENTS.md` §11.1).
- 읽기 시점: dev-pipeline 또는 task 시작 시 가장 먼저 읽는다.
- 작성 원칙: 대화로만 결정된 사항도 반드시 이 문서에 남긴다. 파일에 없는 맥락은 전환 시 소실된 것으로 간주한다.

---

## 양식

아래 양식을 복사해 사용한다. `<>` 부분을 채우고, 해당 없는 섹션은 "해당 없음"으로 남긴다(삭제하지 않는다).

```md
# Checkpoint - <TASK-ID>

## Handoff
- From Agent: <claude-code | codex | 기타>
- To Agent: <claude-code | codex | any>
- Handoff At: <YYYY-MM-DDTHH:MM:SS+09:00>
- Reason: <단계 완료 | 작업 단위 완료 | 위험 작업 직전 | 컨텍스트 압축 대비 | 사용자 중단 | 에이전트 전환 | 사용량 한도 | 오류>
- Status: <in-progress | blocked | done>
- 다음 에이전트가 가장 먼저 할 일:
  1. <구체적인 첫 행동>
  2. <두 번째 행동>

## Task
- ID: <TASK-ID>
- Title: <작업 제목>
- Priority: <high | medium | low>
- Workstreams: <frontend, backend, db, infra, domain 중 해당 항목>
- Requirement IDs: <REQ-*>

## Scope
- <이번 작업 범위 요약, 항목별>

## Acceptance Criteria
- <AC 항목과 현재 충족 여부: 충족 | 미충족 | 미검증>

## Progress
| 단계 | 상태 | 산출물 |
|---|---|---|
| 01-planner | <done/skipped/pending> | <outputs/01-planner-output.md 또는 -> |
| ... | ... | ... |

- Current Step: <진행 중이거나 다음에 실행할 단계 ID>
- Reentry Step: <재진입 시 시작할 단계 ID>

## Key Decisions
- <대화/작업 중 확정된 설계·정책 결정과 근거. 파일로 남지 않은 결정은 반드시 여기에 기록>

## Changed Files
| 파일 | 변경 내용 | 검증 상태 |
|---|---|---|
| <경로> | <요약> | <lint/test/build 결과 또는 미검증> |

## Open Issues
| 심각도 | 이슈 | 담당 역할 | 관련 파일 |
|---|---|---|---|
| <Critical/High/Medium/Low> | <내용> | <owning agent/역할> | <경로> |

## Verification
- 실행한 명령과 결과: <명령 → 결과>
- 실행하지 못한 검증과 이유: <내용>

## User Feedback
- <사용자 테스트 피드백이 있으면 원문 요약과 처리 상태>

## Run Artifacts
- Run Directory: `output/<주제>/<TASK-ID>`
- 주요 산출물: <outputs/, docs/, issues/ 하위 핵심 파일 목록>

## Harness Control

```json
{
  "handoff_status": "<in-progress | blocked | issue | done>",
  "owning_agent_id": "<다음 담당 역할 또는 빈 문자열>",
  "reentry_step_id": "<재진입 단계 ID>",
  "summary": "<한 문장 요약>"
}
```
```

---

## 작성 규칙

1. **Handoff 섹션이 가장 중요하다.** 다음 에이전트는 이 섹션만 읽고도 첫 행동을 시작할 수 있어야 한다.
2. 갱신할 때마다 이전 내용을 `checkpoints/<YYYYMMDD-HHMMSS>-checkpoint.md`로 복사해 이력을 보존한 뒤 `latest-checkpoint.md`를 덮어쓴다.
3. `handoff_status`와 `owning_agent_id`는 이 문서의 Harness Control JSON에 기록한다. 역할 파일(`agent/security.md`, `agent/test-automation.md`)이 말하는 handoff_status 설정 위치가 이 파일이다.
4. 시크릿 값, API 키, 계정 정보는 기록하지 않는다.
5. 검증하지 못한 항목을 완료로 표기하지 않는다.
