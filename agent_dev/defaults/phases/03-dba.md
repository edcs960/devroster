# 03 DBA Phase

## 목적

데이터 모델, 제약조건, 인덱스, 트랜잭션, Revision/Audit/History, 마이그레이션 관점에서 설계를 확정한다.

## 실행 조건

- `workstream:db`
- 또는 API/도메인 변경이 DB 스키마, 인덱스, 이력, 감사 로그, 대량 조회 성능에 영향을 주는 경우

## 적용 역할

- `AGENTS.md`
- `agent/dba.md`

## 입력

- Planner 결과
- Architect 결과
- 데이터정의서, 테이블정의서, 정책정의서, API명세서

## 수행 절차

1. 저장해야 하는 데이터와 기준/트랜잭션/이력 데이터를 분리한다.
2. 엔티티, 관계, PK/FK, Unique, Check, Not Null을 정의한다.
3. 조회 패턴에 맞는 인덱스를 제안한다.
4. 트랜잭션 경계와 동시성 제어 방식을 정리한다.
5. 마이그레이션과 데이터 보정 계획을 작성한다.
6. DB 변경이 없으면 "DB 변경 없음"과 이유를 명시한다.

## 산출물 형식

```md
# DBA 결과

## 1. DB 영향 판단
- DB 변경: Yes/No
- 판단 이유:

## 2. 데이터 도메인 요약
- Master Data:
- Transaction Data:
- History/Audit Data:

## 3. 엔티티/테이블 설계
| 엔티티 | 테이블 | 목적 | 주요 컬럼 |
|---|---|---|---|
| ... | ... | ... | ... |

## 4. 관계 설계
| From | To | 관계 | 정책 |
|---|---|---|---|
| ... | ... | 1:N | ... |

## 5. 제약조건
- PK:
- FK:
- Unique:
- Check:
- Not Null:
- Soft Delete:

## 6. 인덱스 제안
| 테이블 | 인덱스 컬럼 | 목적 | 주의사항 |
|---|---|---|---|
| ... | ... | ... | ... |

## 7. 트랜잭션/동시성 전략
- 트랜잭션 경계:
- 동시 수정 방지:
- Locking/Optimistic Version:
- 실패 시 롤백 기준:

## 8. Revision / Audit / History 전략
- Revision:
- Audit Log:
- 변경 이력:
- 승인 이력:

## 9. 마이그레이션 계획
1. ...
2. ...
3. ...

## 10. 다음 Phase 전달 사항
- Developer:
- QA:
- Reviewer:
```

## 완료 기준

- DB 변경 여부가 명확하다.
- Developer가 마이그레이션과 모델 구현을 시작할 수 있다.

## Harness Control JSON

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "DBA phase completed."
}
```
