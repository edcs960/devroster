# 02 Architect Phase

## 목적

요구사항을 기존 코드베이스에 맞는 기술 설계로 변환하고, 변경 범위, 책임 분리, 데이터/API/상태 흐름을 확정한다.

## 적용 역할

- `AGENTS.md`
- `agent/architect.md`
- `.agents/skills/dev-pipeline/SKILL.md`

## 입력

- Planner 결과
- 프로젝트 구조와 관련 코드
- 인터페이스설계서, 엔티티관계모형기술서, 사용자인터페이스설계서, 개발환경 문서

## 수행 절차

1. 기존 구조, 라우팅, API, 상태관리, 테스트 위치를 확인한다.
2. 변경 영향 범위를 UI, API, Service, Repository, DB, Test로 나눈다.
3. 데이터 흐름과 상태 흐름을 설계한다.
4. API/타입/스키마 계약 변경 필요 여부를 판단한다.
5. 구현 순서와 리스크를 Developer가 실행 가능한 단위로 정리한다.

## 산출물 형식

````md
# Architect 결과

## 1. 현재 구조 분석
- Frontend:
- Backend:
- DB:
- Infra:

## 2. 변경 영향 범위
| 영역 | 변경 여부 | 대상 | 이유 |
|---|---|---|---|
| UI | Yes/No | ... | ... |
| API | Yes/No | ... | ... |
| DB | Yes/No | ... | ... |

## 3. 제안 아키텍처
- 선택한 구조:
- 선택 이유:
- 대안과 배제 이유:

## 4. 데이터/상태 흐름
```txt
사용자 액션 -> UI 상태 변경 -> API/서비스 호출 -> 응답 처리 -> 화면 반영
```

## 5. API/타입/스키마 계약
- Request:
- Response:
- Error:
- Type/DTO:
- Validation:

## 6. 보안/권한/감사 고려사항
- 인증:
- 권한:
- 입력 검증:
- 민감 정보:
- 감사 로그:

## 7. 구현 순서
1. ...
2. ...
3. ...

## 8. 테스트 전략
- Unit:
- Integration:
- E2E:
- Regression:

## 9. 리스크와 대응
| 리스크 | 영향도 | 가능성 | 대응 |
|---|---:|---:|---|
| ... | High/Medium/Low | High/Medium/Low | ... |

## 10. 다음 Phase 전달 사항
- DBA:
- UX/UI:
- Designer:
- Developer:
````

## 완료 기준

- Developer가 파일 단위로 구현 계획을 세울 수 있다.
- 계약, 책임, 리스크가 명확하다.

## Harness Control JSON

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "Architecture phase completed."
}
```
