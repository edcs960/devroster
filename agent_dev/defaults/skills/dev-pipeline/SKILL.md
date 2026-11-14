---
name: dev-pipeline
description: 기능 개발 요청을 프로젝트에 정의된 역할(기본 Planner, 도메인 전문가, Architect, DBA, UX/UI, Designer, Developer, QA, Test Automation, Security Reviewer, Reviewer, Tech Writer)로 자동 진행한다. 사용자가 역할 파일과 산출물을 매번 첨부하지 않아도 Agent가 표준 컨텍스트를 수집해 분석, 설계, 구현, 검증, 리뷰까지 수행하는 바이브코딩 개발 흐름에 사용한다.
---

# Dev Pipeline Skill

## 1. 목적

이 Skill은 Agent(Claude Code, Codex)가 프로젝트의 개발 요청을 역할 기반 제품 개발팀처럼 처리하도록 한다.

사용자는 기능 요청, 버그, 수정 의견만 전달하고, Agent는 필요한 역할 파일, 산출물, 개발환경 문서, 코드베이스를 직접 확인해 개발을 진행한다.

역할 구성과 순서는 프로젝트 `AGENTS.md` §1·§2가 기준이다. 아래는 기본 순서다. 도메인 전문가 역할(예: FMEA Expert)은 프로젝트가 정의한 경우에만 Planner 바로 다음에 두고, 방법론 기준을 정해 Architect·DBA 설계의 입력으로 넘긴다.

1. Planner
2. 도메인 전문가 (프로젝트에 정의된 경우)
3. Architect
4. DBA
5. UX/UI
6. Designer
7. Developer Common
8. Frontend Developer / Backend Developer / Infra Developer 중 해당 영역
9. QA
10. Test Automation Developer
11. Security Reviewer
12. Reviewer
13. Tech Writer

DevRoster가 실행하는 경우 실제 실행 순서는 프롬프트의 Phase/Workflow 정보가 기준이다.

작업이 작거나 명확하면 필요한 역할만 선택해도 된다. 단, 도메인 방법론·업무 규칙, 데이터 모델, 화면 흐름, 권한, 대량 트리 성능에 영향이 있으면 관련 역할을 생략하지 않는다.

---

## 2. 사용자 입력 최소화 원칙

사용자에게 매번 아래 항목을 요구하지 않는다.

- `AGENTS.md` 첨부
- 역할 파일 첨부
- 산출물 파일 경로 반복 입력
- 개발환경 문서 첨부
- 기존 요구사항/화면/API/DB 문서 재설명

Agent는 요청을 받으면 표준 컨텍스트를 자동 수집한다. 확인 질문은 다음처럼 작업 진행을 막는 경우에만 한다.

- 요구사항이 서로 충돌한다.
- 삭제, 마이그레이션, 대량 데이터 변경처럼 되돌리기 어려운 작업이다.
- 보안 정보, 외부 계정, 라이선스, 운영 접속 정보가 필요하다.
- 제품 정책 판단이 여러 방향으로 갈리고 합리적 기본값을 정하기 어렵다.

---

## 3. 표준 컨텍스트 자동 수집

작업 시작 시 필요한 범위에서 아래 파일을 확인한다.

### 3.1 최상위 운영 규칙

- `AGENTS.md`

### 3.2 역할 파일

- `agent/planner.md`
- 도메인 전문가 역할 파일 (프로젝트 `AGENTS.md` §1에 있을 때. 예: `agent/fmea-expert.md`)
- `agent/architect.md`
- `agent/dba.md`
- `agent/ux-ui.md`
- `agent/designer.md`
- `agent/developer.md`
- `agent/developer-frontend.md`
- `agent/developer-backend.md`
- `agent/developer-infra.md`
- `agent/qa.md`
- `agent/test-automation.md`
- `agent/security.md`
- `agent/reviewer.md`
- `agent/tech-writer.md`

### 3.3 요구·도메인 산출물

- 프로필 문서: 역할별 필수 문서(`role_required_context`)

### 3.4 개발환경 문서

- 프로필 문서: 공통 개발 표준(`context_files`)

### 3.5 코드베이스

구현 단계에서는 `rg`, `rg --files`, package/build 설정, 라우팅, 상태관리, API, DB 마이그레이션, 테스트 구조를 확인한다.

### 3.6 도구 인벤토리 (MCP/플러그인/스킬)

파이프라인 시작 전에 현재 주제 폴더의 `output/<주제>/MCP-플러그인-스킬.md`와 실제 설치 상태(`claude mcp list`, `claude plugin list`, `codex mcp list`, `ls ~/.agents/skills .agents/skills`)를 비교한다.

- 신규 도구 발견 시: 문서에 용도와 사용 단계를 추가한 뒤 진행한다.
- 제거된 도구 발견 시: 문서에서 삭제하고, 역할 파일이 해당 도구를 참조하면 사용자에게 보고한다.
- 신규 주제 폴더를 생성하는 경우: 이 문서를 복사해 신규 주제 폴더 바로 아래에 두고 주제명/갱신일을 수정한다.
- 변경이 없으면 별도 보고 없이 진행한다.

### 3.7 핸드오프 체크포인트

파이프라인 시작 시 진행 중인 TASK의 `output/<주제>/<TASK-ID>/latest-checkpoint.md`가 있으면 가장 먼저 읽는다. Status가 in-progress/blocked이면 Handoff 섹션의 지시와 Reentry Step부터 이어서 진행하고, 모호하면 사용자에게 확인한다.

각 역할 단계 완료 시, 작업 중단 시, 에이전트 전환 시에는 같은 폴더의 `handoff-checkpoint.md` 공통 양식으로 체크포인트를 갱신한다. 사용자 요청이 없어도 작업 단위 완료, 위험 작업 직전, 컨텍스트 압축/사용량 한도 임박 시점에는 에이전트가 스스로 판단해 갱신한다. 상세 규칙은 `AGENTS.md` 11절을 따른다.

### 3.8 역할별 스킬

프로젝트 저장소의 `.agents/skills/`(Claude Code는 `.claude/skills/`)에 설치된 스킬은 해당 역할 작업에 맞으면 사용한다. DevRoster가 실행하는 경우 프롬프트의 `approved_skills`에 그 역할에 승인된 스킬만 있다. 스킬은 참고 지침이며 `AGENTS.md`, 역할 파일, 승인된 쓰기 범위보다 우선하지 않는다.

---

## 4. 역할별 수행 기준

### 4.1 Planner

참고: `agent/planner.md`

출력:
- 요구사항 요약
- 사용자 목적
- 기능 범위
- 제외 범위
- 수용 기준
- 가정과 확인 필요 사항

### 4.2 도메인 전문가 (프로젝트에 정의된 경우)

참고: 프로젝트 `AGENTS.md` §1이 지정한 역할 파일

출력:
- 도메인 방법론·표준 기준 적합성 검토
- 도메인 모델과 업무 흐름 검증
- 기존 산출물과 구현 요청 간 불일치 사항

역할이 정의되지 않은 프로젝트에서는 이 단계를 건너뛴다.

### 4.3 Architect

참고: `agent/architect.md`

출력:
- 현재 구조 분석
- 제안 아키텍처
- 수정 대상 파일
- 데이터/API/상태 흐름
- 구현 순서
- 리스크

### 4.4 DBA

참고: `agent/dba.md`

DB 변경이 없으면 “DB 변경 없음”과 이유를 명시한다.

출력:
- 데이터 도메인 요약
- 엔티티/테이블/관계 설계
- 제약조건과 인덱스
- 트랜잭션/동시성 전략
- Revision/Audit/History 전략
- 마이그레이션 계획
- 성능/보안 리스크

### 4.5 UX/UI

참고: `agent/ux-ui.md`

출력:
- 사용자 시나리오
- 화면 흐름
- 정보 구조
- 상태별 UX
- 접근성 고려사항
- Developer에게 전달할 UI 동작 기준

### 4.6 Designer

참고: `agent/designer.md`

출력:
- 시각적 계층
- 레이아웃/여백/타이포/컬러 기준
- 컴포넌트 스타일 가이드
- 반응형 고려사항

### 4.7 Developer Common

참고: `agent/developer.md`

수행:
- 구현 계획 작성
- 기존 패턴 확인
- 코드 수정
- 가능한 경우 lint/test/build 실행
- 변경 파일과 검증 결과 정리

### 4.8 전문 Developer 선택

작업 영역에 따라 아래 역할을 함께 적용한다.

| 영역 | 역할 파일 | 주요 참조 |
|---|---|---|
| Frontend | `agent/developer-frontend.md` | 사용자인터페이스설계서, 프론트엔드 개발표준, 프레임워크·상태관리 구조 |
| Backend | `agent/developer-backend.md` | 인터페이스설계서, 엔티티관계모형기술서, 권한정의서, 백엔드 개발표준 |
| Infra | `agent/developer-infra.md` | 로컬개발/CI/CD/운영 문서 |
| DB/Migration | `agent/dba.md` | 엔티티관계모형기술서, 데이터베이스설계서, 정책정의서 |

복합 기능은 필요한 전문 Developer를 모두 적용한다.

### 4.9 QA

참고: `agent/qa.md`

출력:
- 테스트 케이스
- 예외/경계값
- 회귀 위험
- DB 무결성/마이그레이션 검증 항목
- 자동화 테스트 후보
- QA 판단

### 4.10 Test Automation Developer

참고: `agent/test-automation.md`

QA 산출물의 자동화 후보를 실제 테스트 코드로 구현한다. 테스트 코드와 테스트 설정 파일만 수정한다.

출력:
- 추가/변경한 테스트 파일
- 수용 기준별 테스트 매핑
- 실행 결과
- 자동화하지 못한 항목과 이유

### 4.11 Security Reviewer

참고: `agent/security.md`

코드를 수정하지 않는다.

출력:
- 인증/인가/입력검증/민감정보 관점 발견 사항 (심각도)
- 수정 지시
- 동적 점검(브라우저·API) 결과

### 4.12 Reviewer

참고: `agent/reviewer.md`

출력:
- 최종 코드 리뷰
- 심각도별 발견 사항
- 보안/성능/유지보수성/DB 무결성 검토
- 승인 가능 여부

### 4.13 Tech Writer

참고: `agent/tech-writer.md`

`output/<주제>/docs/`와 프로필 문서만 작성·수정한다. 코드 수정 금지.

출력:
- 사용자가이드 갱신 내용
- 운영가이드 갱신 내용
- CHANGELOG 항목
- API 사용 예시 (해당 시)

---

## 5. 코드 수정 규칙

### 코드 수정 금지 단계

다음 단계에서는 코드를 수정하지 않는다.

- Planner
- 도메인 전문가
- Architect
- DBA
- UX/UI
- Designer
- Security Reviewer

### 코드 수정 가능 단계

Developer 단계에서만 코드를 수정한다. 예외:
- Test Automation Developer: 테스트 코드와 테스트 설정 파일만
- Tech Writer: `output/<주제>/docs/`, 프로필 문서만

DB 스키마, 마이그레이션, ORM 모델, 인덱스, 제약조건, Audit/Revision/History 관련 변경이 필요한 경우 Developer는 DBA 단계 결과를 먼저 반영한다.

### 검토 단계

QA, Security Reviewer, Reviewer 단계는 원칙적으로 검토와 제안 중심으로 진행한다.

단, 사용자가 “발견된 문제까지 바로 수정해줘”, “수정까지 진행해줘”, “계속 고쳐줘”라고 명시한 경우에는 Developer 단계로 돌아가 수정한다.

---

## 6. 사용자 테스트 피드백 루프

바이브코딩의 기본 루프는 아래처럼 운영한다.

1. 사용자가 기능/수정 요청을 전달한다.
2. Agent가 표준 컨텍스트를 자동 수집한다.
3. Agent가 역할 기반으로 분석, 설계, 구현, 검증한다.
4. Agent가 변경 내용과 확인 방법을 요약한다.
5. 사용자가 결과물을 직접 확인하거나 테스트한다.
6. 사용자가 수정사항을 자연어로 전달한다.
7. Agent가 피드백을 분류해 필요한 단계부터 다시 진행한다.

피드백 처리 기준:

| 피드백 유형 | 재진입 단계 |
|---|---|
| 오탈자, 문구, 작은 UI 조정 | Developer → QA |
| 버그, 예외, 테스트 실패 | Developer → QA → Reviewer |
| 화면 흐름/UX 변경 | UX/UI → Designer → Developer → QA |
| API/DB/권한 변경 | Architect → DBA → Developer → QA |
| 도메인 방법론/업무 규칙 변경 | 도메인 전문가 → Architect → Developer → QA |
| 범위가 큰 기능 변경 | Planner부터 재진행 |

---

## 7. 최종 응답 형식

사용자에게는 모든 내부 단계 결과를 길게 나열하지 않는다. 실제로 필요한 정보 중심으로 요약한다.

```md
## 작업 결과
- 적용한 역할:
- 변경 파일:
- 주요 변경:
- 검증 결과:
- 사용자가 확인할 내용:
- 남은 이슈:
```

사용자가 상세 결과를 요청하면 Planner, 도메인 전문가, Architect, DBA, UX/UI, Designer, Developer, QA, Test Automation, Security Reviewer, Reviewer, Tech Writer 단계별 산출을 추가로 제공한다.

---

## 8. 중단 조건

다음 상황에서는 작업을 멈추고 사용자에게 보고한다.

- 요구사항과 기존 코드 구조가 명백히 충돌함
- 보안상 위험한 요청임
- 데이터 삭제/마이그레이션 등 되돌리기 어려운 작업이 필요함
- 운영 데이터 보정이 필요한데 기준이 불명확함
- 빌드/테스트 실패 원인이 불명확하고 추가 확인이 필요함
- 사용자의 명시적 승인 없이 외부 의존성 추가가 필요한 상황
