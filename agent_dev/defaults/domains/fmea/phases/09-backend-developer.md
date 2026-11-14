# 09 Backend Developer Phase

## 목적

Spring Boot 기반 API, 도메인 서비스, 권한, 검증, Audit/Revision 흐름을 실제 코드로 구현한다.

## 실행 조건

- `workstream:backend`

## 적용 역할

- `AGENTS.md`
- `agent/developer.md`
- `agent/developer-backend.md`
- 필요 시 `agent/dba.md`

## 입력

- Developer Common 결과
- Architect 결과
- DBA 결과
- 인터페이스설계서, 엔티티관계모형기술서, 권한정의서, 백엔드 개발표준

## 수행 절차

1. Controller, Service, Repository, Entity/DTO/Mapper 구조를 확인한다.
2. API 계약, 권한, 검증, 트랜잭션 경계를 구현한다.
3. DB 변경이 있으면 DBA 결과에 따라 마이그레이션을 반영한다.
4. 오류 코드, Audit Log, Revision Lock 정책을 맞춘다.
5. 가능한 경우 test, integrationTest, bootJar를 실행한다.

## 산출물 형식

```md
# Backend Developer 결과

## 1. 구현 요약
- ...

## 2. 변경 파일
| 파일 | 변경 내용 | 이유 |
|---|---|---|
| ... | ... | ... |

## 3. API/도메인 규칙
- Endpoint:
- Validation:
- Permission:
- Transaction:
- Audit:

## 4. DB 영향
- Migration:
- Entity:
- Index:
- History/Audit:

## 5. 오류/예외 처리
- Error Code:
- Validation Error:
- Permission Error:

## 6. 검증 결과
| 명령 | 결과 | 비고 |
|---|---|---|
| ./gradlew test | 성공/실패/미실행 | ... |
| ./gradlew integrationTest | 성공/실패/미실행 | ... |
| ./gradlew bootJar | 성공/실패/미실행 | ... |

## 7. 다음 Phase 전달 사항
- Frontend:
- QA:
- Reviewer:
```

## 완료 기준

- API 계약과 권한/검증 정책이 구현되어 있다.
- 테스트 또는 미검증 사유가 명확하다.

## Harness Control JSON

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "Backend implementation completed."
}
```
