# 08 Frontend Developer Phase

## 목적

React 기반 화면, 상태관리, API 연동, UI 상태, 테스트를 실제 코드로 구현한다.

## 실행 조건

- `workstream:frontend`

## 적용 역할

- `AGENTS.md`
- `agent/developer.md`
- `agent/developer-frontend.md`
- UX/UI 결과
- Designer 결과

## 입력

- Developer Common 결과
- 화면정의서, API명세서, 프론트엔드 개발표준
- 기존 라우팅, 공통 컴포넌트, API client, Store, 테스트 구조
- UX/UI 또는 Designer 단계에서 확인한 디자인 참고 자료와 구현 기준

## 수행 절차

1. 라우팅, 페이지, 컴포넌트, 상태관리, API client 패턴을 확인한다.
2. 화면과 상태를 최소 변경으로 구현한다.
3. Loading, Empty, Error, Read-only, Dirty, Permission 상태를 반영한다.
4. API 계약과 타입을 맞춘다.
5. `Design Reference Context`가 있으면 참고 화면을 구현 기준으로 확인하고, 구현이 다른 부분은 UX/UI·Designer 지시, 화면정의서, 기존 디자인 시스템 기준으로 사유를 설명한다.
6. 가능한 경우 lint, typecheck, test, build를 실행한다.

## 산출물 형식

```md
# Frontend Developer 결과

## 1. 구현 요약
- ...

## 2. 변경 파일
| 파일 | 변경 내용 | 이유 |
|---|---|---|
| ... | ... | ... |

## 3. 주요 UI/상태 처리
- Loading:
- Empty:
- Error:
- Read-only:
- Dirty:
- Permission:

## 4. API 연동
- Query:
- Mutation:
- Error Code:
- Type/DTO:

## 5. 접근성/반응형 반영
- 키보드:
- 스크린리더:
- 모바일:

## 6. 디자인 참고 자료 비교 결과

`Design Reference Context`가 있는 경우 이 섹션은 필수다.

- 참조한 참고 자료 ID와 출처(Stitch/Figma)
- 참조한 화면/노드
- 구현에 반영한 화면 요소
- 의도적으로 다르게 구현한 부분과 이유
- 사용 여부: Yes/No
- 비교 대상:
- 일치한 부분:
- 다르게 구현한 부분과 이유:
- 미확인 이유:

## 7. 검증 결과
| 명령 | 결과 | 비고 |
|---|---|---|
| pnpm lint | 성공/실패/미실행 | ... |
| pnpm typecheck | 성공/실패/미실행 | ... |
| pnpm test | 성공/실패/미실행 | ... |
| pnpm build | 성공/실패/미실행 | ... |

## 8. 다음 Phase 전달 사항
- Backend:
- QA:
- Reviewer:
```

## 완료 기준

- 수용 기준에 맞는 UI 동작이 구현되어 있다.
- 미실행 검증과 이유가 명확하다.

## Harness Control JSON

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "Frontend implementation completed."
}
```
