# 10 Infra Developer Phase

## 목적

로컬 개발 환경, Docker, CI/CD, 환경변수, 배포, 로그/모니터링, 보안 설정을 구현한다.

## 실행 조건

- `workstream:infra`
- 또는 실행 환경, CI, Secret, 배포 설정이 변경되는 경우

## 적용 역할

- `AGENTS.md`
- `agent/developer.md`
- `agent/developer-infra.md`

## 입력

- Developer Common 결과
- 로컬개발/CI/CD/운영 문서
- Docker/CI/env 관련 기존 파일

## 수행 절차

1. 기존 로컬 실행 방식과 CI 설정을 확인한다.
2. 환경변수와 Secret 관리 방식을 정리한다.
3. Docker Compose, Dockerfile, workflow, health check 변경을 구현한다.
4. 가능한 경우 build, test job, health check를 실행한다.
5. 운영/보안 주의사항을 남긴다.

## 산출물 형식

```md
# Infra Developer 결과

## 1. 구현 요약
- ...

## 2. 변경 파일
| 파일 | 변경 내용 | 이유 |
|---|---|---|
| ... | ... | ... |

## 3. 실행 방법
- ...

## 4. 환경변수/Secret
| 이름 | 필수 | 설명 | 보관 위치 |
|---|---|---|---|
| ... | Yes/No | ... | ... |

## 5. 검증 결과
| 명령 | 결과 | 비고 |
|---|---|---|
| docker compose ... | 성공/실패/미실행 | ... |
| pnpm --dir frontend build | 성공/실패/미실행 | ... |
| ./gradlew -p backend bootJar | 성공/실패/미실행 | ... |

## 6. 운영/보안 주의사항
- ...

## 7. 다음 Phase 전달 사항
- QA:
- Reviewer:
```

## 완료 기준

- 실행/배포/검증 방법이 재현 가능하다.
- Secret이 하드코딩되지 않는다.

## Harness Control JSON

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "Infra implementation completed."
}
```
