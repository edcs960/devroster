# 07 Developer Common Phase

## 목적

설계 산출물을 실제 구현 계획으로 변환하고, 기존 코드 패턴을 확인한 뒤 구현 범위와 검증 방법을 확정한다.

## 적용 역할

- `AGENTS.md`
- `agent/developer.md`
- 작업 영역에 따라 전문 Developer 역할 파일

## 입력

- Planner, 도메인 전문가(있을 때), Architect, DBA, UX/UI, Designer 결과
- 관련 코드베이스
- 개발환경 문서

## 수행 절차

1. 관련 파일과 기존 구현 패턴을 확인한다.
2. 변경할 파일과 변경 이유를 정리한다.
3. 구현 순서를 최소 변경 단위로 나눈다.
4. 리스크와 검증 방법을 정리한다.
5. 전문 Developer 단계가 이어받을 작업을 분배한다.

## 산출물 형식

```md
# Developer Common 결과

## 1. 구현 요약
- ...

## 2. 기존 패턴 확인
- Frontend:
- Backend:
- DB:
- Infra:

## 3. 구현 계획
| 순서 | 영역 | 파일/모듈 | 작업 | 리스크 |
|---:|---|---|---|---|
| 1 | frontend/backend/db/infra | ... | ... | ... |

## 4. 변경 파일 후보
| 파일 | 변경 이유 |
|---|---|
| ... | ... |

## 5. 전문 Developer 전달 사항
- Frontend:
- Backend:
- Infra:
- DB/Migration:

## 6. 검증 계획
- Lint:
- Typecheck:
- Unit:
- Integration:
- E2E:
- Build:

## 7. 미확정/리스크
- ...
```

## 완료 기준

- 코드 수정 직전의 작업 단위가 명확하다.
- 전문 Developer가 바로 구현할 수 있다.

## Harness Control JSON

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "Developer planning completed."
}
```
