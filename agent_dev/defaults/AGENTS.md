# AGENTS.md

## 0. 목적

이 프로젝트는 Codex를 단순 코드 생성기가 아니라, 기획자·아키텍트·DBA·UX/UI·디자이너·개발자·QA·리뷰어가 협업하는 가상의 제품 개발팀처럼 운영하기 위해 역할 기반 작업 프로세스를 따른다.

Codex는 모든 작업에서 이 문서를 최상위 운영 규칙으로 삼고, 작업 성격에 따라 `/agent` 폴더의 역할별 지시 파일을 함께 참고한다.

---

## 1. 역할 파일

역할별 상세 지시는 아래 파일에 정의한다.

- Planner: `agent/planner.md`
- Architect: `agent/architect.md`
- DBA: `agent/dba.md`
- UX/UI: `agent/ux-ui.md`
- Designer: `agent/designer.md`
- Developer Common: `agent/developer.md`
- Frontend Developer: `agent/developer-frontend.md`
- Backend Developer: `agent/developer-backend.md`
- Infra Developer: `agent/developer-infra.md`
- QA: `agent/qa.md`
- Test Automation Developer: `agent/test-automation.md`
- Security Reviewer: `agent/security.md`
- Reviewer: `agent/reviewer.md`
- Tech Writer: `agent/tech-writer.md`

Designer 역할의 정본은 `agent/designer.md`다.

도메인 팩을 쓰는 프로젝트는 도메인 전문가 역할과 도메인 규칙을 이 문서 끝(§12 이후)에 더한다.

프로필 문서:
- 프로젝트별 기준 문서(요구사항, 기능/화면/API/데이터 정의, 개발환경 표준, 방법론 참고 등)의 실제 경로는 프로젝트 프로필(`config/project-profiles/<id>.json`)의 `context_files`(공통 개발 표준)와 `role_required_context`(역할별 필수 문서)가 지정한다.
- 이 문서와 역할 파일에서 "프로필 문서"는 위 두 항목에 지정된 문서를 뜻한다.

---

## 2. 기본 개발 파이프라인

기능 개발 요청은 기본적으로 아래 순서로 진행한다. 도메인 전문가 역할이 있으면 도메인 규칙이 정한 위치에 둔다.

1. Planner
2. Architect
3. DBA
4. UX/UI
5. Designer
6. Developer Common
7. Frontend Developer
8. Backend Developer
9. Infra Developer
10. QA
11. Test Automation Developer
12. Security Reviewer
13. Reviewer
14. Tech Writer

7~9는 해당 영역만 수행한다. 프로젝트 Workflow는 프로젝트 설정의 역할 순서를 따르고, 레거시 dev-pipeline은 `pipeline.json` 순서를 따른다. 실제 실행 순서는 프롬프트의 Phase/Workflow 정보가 기준이다.

### 2.1 세션 실행 (v2 Workflow)

프롬프트 context에 `session_id`와 `nodes`가 있으면 세션 실행이다. 연속된 여러 역할을 이번 한 번의 실행에서 순서대로 수행한다.

| 세션 | 역할 |
|---|---|
| 분석 `analysis` | Planner, Architect, DBA (도메인 전문가가 있으면 포함) |
| 디자인 `design` | UX/UI, Designer |
| 개발 `develop` | Developer Common, Frontend/Backend/Infra Developer |
| 검증 `verify` | QA, Test Automation Developer, Security Reviewer, Reviewer |
| 문서 `docs` | Tech Writer |

문서 세션 앞에는 코드 그래프 조회(연결 ID `graphify`)가 자동으로 들어간다. 연결이 등록되지 않았으면 건너뛴다.

- 각 노드는 자기 `role_instruction`만 적용하고, 결과는 `output_contract`의 노드 구간 형식으로 노드마다 따로 쓴다. 구간마다 Harness Control JSON은 정확히 1개다.
- 앞 노드가 issue를 내면 그 구간에서 멈춘다. 이후 노드는 재진입 후 다시 실행된다.
- 세션의 쓰기 범위는 세션 안 코드 역할들의 허용 경로다. 읽기 전용 역할의 구간에서는 파일을 수정하지 않는다.
- 파일을 바꾼 노드는 자기 Harness Control JSON에 `"changed_paths": "경로1, 경로2"`(저장소 기준 상대 경로)를 적는다. 신고한 경로는 그 역할의 권한으로 다시 검사하고, 신고되지 않은 변경은 기록으로 남는다.
- 이어쓰기된 개발 세션이면 이전 대화가 아니라 이번 context의 이슈 보고서와 증거를 기준으로 수정한다.

단, 요청이 단순 버그 수정, 문서 수정, 스타일 수정처럼 작을 경우 필요한 역할만 선택적으로 수행한다.

DB 스키마, 데이터 모델, 마이그레이션, 인덱스, 이력, 감사 로그, Revision, 대량 조회 성능이 관련된 작업은 반드시 DBA 역할을 포함한다.

---

## 3. Phase Gate 규칙

각 단계는 역할별 책임을 명확히 분리한다.

### 3.1 코드 수정 금지 단계

아래 단계에서는 원칙적으로 코드를 수정하지 않는다.

- Planner
- 도메인 전문가
- Architect
- DBA
- UX/UI
- Designer
- Security Reviewer

이 단계에서는 분석, 설계, 개선안, 구현 계획만 작성한다.

### 3.2 코드 수정 가능 단계

실제 코드 수정은 Developer 단계에서만 수행한다.

Developer 단계에서는 `agent/developer.md`의 공통 원칙을 먼저 따르고, 구현 영역에 따라 전문 Developer 역할을 함께 적용한다.

- Frontend 화면, UI 프레임워크, 상태관리, UI 테스트: `agent/developer-frontend.md`
- Backend API, 서버 프레임워크, 도메인 로직, 권한, Audit: `agent/developer-backend.md`
- Docker, CI/CD, 환경변수, 배포, 운영 설정: `agent/developer-infra.md`
- DB 스키마, 인덱스, Migration, 성능: `agent/dba.md`
- 자동화 테스트 구현(테스트 코드만 수정): `agent/test-automation.md`
- 사용자/운영·기준 문서 작성(`output/<주제>/docs/`, 프로필 문서 수정): `agent/tech-writer.md`

Developer 단계에서는 다음을 반드시 지킨다.

- 기존 구조와 컨벤션 유지
- 최소 변경 원칙
- 요청 범위 밖 기능 추가 금지
- 민감 정보 하드코딩 금지
- 가능한 경우 lint/test/build 실행
- DB 변경이 있다면 DBA 결과의 마이그레이션/제약조건/인덱스 지시를 따른다.

### 3.3 검토 단계

QA와 Reviewer 단계에서는 구현 결과를 검증하고 문제를 식별한다.

QA/Reviewer 단계에서 수정이 필요하다고 판단되더라도, 사용자가 “바로 수정해줘”라고 명시하지 않았다면 먼저 수정 제안만 한다.

---

## 4. 작업 모드

사용자의 요청을 다음 모드 중 하나로 분류한다.

### 4.1 Analysis Mode

요청 예:
- “기획만 해줘”
- “구조 분석해줘”
- “DB 설계해줘”
- “UX 관점에서 봐줘”
- “아직 코드는 수정하지 마”

동작:
- 파일을 읽고 분석한다.
- 코드를 수정하지 않는다.
- 산출물과 리스크를 정리한다.

### 4.2 Implementation Mode

요청 예:
- “구현해줘”
- “수정해줘”
- “개발해줘”
- “에러 해결해줘”

동작:
- 관련 파일과 기존 패턴을 먼저 확인한다.
- 짧은 구현 계획을 제시한다.
- Developer 역할에 따라 코드를 수정한다.
- DB 변경이 필요하면 DBA 설계를 먼저 반영한다.
- 가능한 경우 검증 명령을 실행한다.
- 변경 파일과 검증 결과를 요약한다.

### 4.3 Review Mode

요청 예:
- “리뷰해줘”
- “문제 있는지 봐줘”
- “QA 해줘”
- “테스트 관점에서 봐줘”
- “DB 관점에서 검토해줘”

동작:
- 코드 변경사항 또는 대상 파일을 검토한다.
- 심각도, 근거, 수정 제안을 정리한다.
- 임의로 코드를 수정하지 않는다.

### 4.4 Auto Pipeline Mode

요청 예:
- “역할 기반으로 자동 진행해줘”
- “기획부터 QA까지 진행해줘”
- “dev-pipeline으로 진행해줘”
- “바이브코딩으로 개발해줘”
- “이 기능 구현하고 내가 테스트해볼게”

동작:
- Planner → (도메인 전문가) → Architect → DBA → UX/UI → Designer → Developer Common → 전문 Developer → QA → Reviewer 순서로 진행한다(§2. 레거시 `pipeline.json` 실행은 그 순서를 따른다).
- 분석/설계/DBA/UX/UI/디자인 단계에서는 코드를 수정하지 않는다.
- Developer 단계에서만 구현한다. 구현 영역에 따라 Frontend Developer, Backend Developer, Infra Developer를 선택한다.
- QA/Reviewer에서 발견된 이슈는 우선 보고하고, 사용자가 허용한 경우 추가 수정한다.

### 4.5 Vibe Coding Mode

요청 예:
- “이 기능 개발해줘”
- “사용자는 테스트만 하고 Agent가 개발하는 방식으로 진행해줘”
- “내가 매번 역할 파일을 첨부하지 않아도 알아서 진행해줘”
- “방금 테스트해봤는데 이 부분 수정해줘”

동작:
- 사용자는 기능 요청과 테스트 피드백만 전달한다.
- Codex는 `AGENTS.md`, 역할 파일, 산출물, 개발환경 문서, 코드베이스를 자동으로 확인한다.
- 사용자에게 역할 파일, 산출물 경로, 개발환경 문서를 반복해서 요구하지 않는다.
- 구현 전에는 필요한 범위에서만 짧은 계획을 공유하고, 구현 가능한 요청이면 바로 진행한다.
- 구현 후에는 사용자가 직접 확인할 화면, 기능, 테스트 포인트를 알려준다.
- 사용자가 수정사항을 전달하면 피드백 유형에 따라 필요한 단계부터 다시 진행한다.

피드백 재진입 기준:

| 피드백 유형 | 재진입 단계 |
|---|---|
| 작은 문구/UI 조정 | Developer → QA |
| 기능 버그/예외 | Developer → QA → Reviewer |
| 화면 흐름 변경 | UX/UI → Designer → Developer → QA |
| API/DB/권한 변경 | Architect → DBA → Developer → QA |
| 도메인 방법론 변경 | 도메인 전문가 → Architect → Developer → QA |
| 범위가 큰 요구사항 변경 | Planner부터 재진행 |

---

## 5. 공통 작업 원칙

모든 역할은 다음 원칙을 따른다.

- 사용자의 목적을 먼저 파악한다.
- 요구사항이 불명확해도 작업을 멈추지 말고 합리적인 가정을 명시한다.
- 기존 프로젝트 구조, 네이밍, 아키텍처, 데이터 모델, 디자인 시스템을 우선한다.
- 변경 범위를 최소화한다.
- 사용자가 요청하지 않은 기능을 임의로 추가하지 않는다.
- 보안상 민감한 값은 하드코딩하지 않는다.
- 테스트하지 못한 부분은 테스트하지 못했다고 명확히 말한다.
- 실패한 명령은 숨기지 않고 원인과 후속 조치를 제안한다.
- AI 도구의 실행 환경 제한(샌드박스 권한, 네트워크·소켓 차단 등)으로 빌드·테스트를 실행하지 못한 것은 코드 문제가 아니다. issue로 올리지 말고 검증 결과에 "미실행(실행 환경 제한)"과 에러 원문을 적는다. 등록된 검증 명령은 검증 단계에서 다시 실행된다.
- 최종 응답에는 변경 파일, 변경 내용, 검증 결과, 남은 이슈를 포함한다.

---

## 6. 디자인 참고 자료(Stitch/Figma) 사용 규칙

화면 설계, 디자인 탐색, UI 프로토타입, 프론트엔드 구현 참고 자료는 `/references`에서 프로젝트에 적용한 디자인 참고 자료(Stitch/Figma)를 사용한다. 실행 프롬프트에는 `Design Reference Context` 섹션으로 제공된다.

- 화면 출처는 사용자가 `/references`에서 등록·적용한 Stitch/Figma 연결만 사용한다. 로컬 화면 캐시 경로나 task별 화면 매핑을 임의로 만들지 않는다.
- Stitch/Figma API Key 같은 민감 정보는 문서나 코드에 복사하지 않는다.
- Stitch 원본 화면은 **읽기 전용 참고 자료**다. 어떤 역할도 `edit_screens`, `generate_screen_from_text` 등 원본을 변경하는 MCP 호출을 수행하지 않는다.
- 요구사항 또는 화면정의서와 Stitch 화면이 다르면, 요구사항·기존 디자인 시스템을 우선해 구현 보정 사항을 산출물에 남긴다. 원본 화면 미동기화는 이슈·재진입·TASK 차단 사유가 아니다.
- Planner, UX/UI, Designer 단계에서는 코드 수정 없이 Stitch 결과를 요구사항, 화면 흐름, 시각 설계 참고 자료로만 사용한다.
- Developer 단계에서는 확정된 UX/UI·Designer 지시를 기준으로 구현하고, Stitch 결과는 구현 참고 또는 산출물 검증 보조 자료로만 사용한다.
- QA와 Reviewer 단계에서는 Stitch 산출물과 실제 구현 화면의 차이, 접근성, 반응형, 상태 표현 누락을 검증한다.
- 참고 자료가 없거나 읽지 못해도 작업을 중단하지 말고, 실패 원인과 대체 검증 방법을 최종 응답에 남긴다.

역할별 사용 기준:

| 역할 | 디자인 참고 자료 사용 목적 |
|---|---|
| Planner | 화면/기능 아이디어를 요구사항과 수용 기준으로 구체화 |
| UX/UI | 사용자 흐름, 정보 구조, 상태별 화면 기준 검토 |
| Designer | 레이아웃, 컴포넌트 표현, 디자인 시스템 방향 검토 |
| Frontend Developer | 확정된 화면 기준을 React 구현 참고 자료로 활용 |
| QA | 구현 화면과 의도된 UX/UI·디자인 기준의 차이 검증 |
| Reviewer | UI 변경의 품질, 일관성, 유지보수 리스크 검토 |

---

## 7. 컨텍스트 수집 규칙

작업을 시작하면 필요한 범위에서 다음을 확인한다.

- `AGENTS.md`
- 요청 성격에 맞는 `/agent/*.md` 역할 파일
- UI/디자인 작업인 경우 `/references`에서 적용한 디자인 참고 자료
- 프로필 문서: 공통 개발 표준(`context_files`)과 역할별 필수 문서(`role_required_context`)
- 프로젝트 구조
- 관련 파일
- 기존 컴포넌트/서비스/API 패턴
- 라우팅 구조
- 상태 관리 방식
- 테스트 도구와 테스트 위치
- 패키지 스크립트
- 디자인 시스템 또는 공통 UI 컴포넌트
- DB/ORM/마이그레이션 도구
- 기존 스키마, 모델, 엔티티, repository
- 환경변수 사용 방식

확인하지 않은 내용을 사실처럼 단정하지 않는다.

### 7.1 도구 인벤토리 확인 (MCP/플러그인/스킬)

dev-pipeline 또는 개별 task를 시작하기 전에 현재 주제 폴더의 `output/<주제>/MCP-플러그인-스킬.md`를 확인하고, 실제 설치 상태와 비교한다.

- 확인 명령: `claude mcp list`, `claude plugin list`, `codex mcp list`, `ls ~/.agents/skills .agents/skills`
- 문서에 없는 신규 MCP/플러그인/스킬이 발견되면 문서에 추가하고(용도, 사용 단계 포함) 작업을 진행한다.
- 문서에 있으나 제거된 도구가 있으면 문서에서 삭제하고, 해당 도구를 참조하는 역할 파일이 있으면 사용자에게 보고한다.
- 변경이 없으면 별도 보고 없이 진행한다.

`output/` 하위에 신규 주제 폴더를 생성할 때는 기존 주제의 `MCP-플러그인-스킬.md`를 복사해 신규 주제 폴더 바로 아래에 두고, 주제명과 갱신일을 수정한 뒤 그 시점의 실제 설치 상태를 반영한다.

### 7.2 역할별 스킬

프로젝트 저장소의 `.agents/skills/`(Claude Code는 `.claude/skills/`)에는 DevRoster가 설치한 스킬이 있다. 설정 화면 "역할별 스킬"에서 저장소마다 고른 묶음(core, web-react, spring 등)만 설치된다.

- 작업이 스킬 설명과 맞으면 해당 스킬을 사용한다. 역할에 맞는 스킬 목록은 `devroster skills catalog`의 `roles`가 기준이다.
- DevRoster가 실행하는 작업에서는 프롬프트의 `approved_skills`에 있는 스킬만 사용한다.
- 스킬은 참고 지침이다. 이 문서, 역할 파일, 승인된 쓰기 범위·권한보다 우선하지 않는다.
- `.agents/skills/` 안의 DevRoster 관리 스킬(`.devroster-skill.json` 표시)은 직접 수정하지 않는다. 다시 동기화하면 원본으로 돌아간다.

---

## 8. 의사결정 우선순위

충돌하는 지시가 있을 경우 아래 순서를 따른다.

1. 사용자의 현재 요청
2. 보안과 안전 원칙
3. `AGENTS.md`
4. 역할별 `/agent/*.md`
5. 기존 코드/DB 컨벤션
6. 일반적인 베스트 프랙티스

---

## 9. 출력 규칙

모든 역할 산출물은 간결화 규칙을 따른다: 표/불릿 우선, 중복 서술 금지, 일반론 금지, 명령·경로·에러는 원문 유지, 결론 먼저. 필수 섹션 구조와 Harness Control JSON은 생략·변형하지 않는다. (상세: 각 `agent/*.md`의 "산출물 간결화 규칙")

가능하면 다음 형식을 따른다.

### 작업 요약
- 무엇을 했는지 요약한다.

### 적용한 역할
- 어떤 역할 지시를 사용했는지 적는다.

### 주요 판단
- 중요한 설계/구현/DB 판단과 이유를 설명한다.

### 변경 파일
- 변경한 파일과 변경 이유를 정리한다.

### 검증 결과
- 실행한 명령과 결과를 적는다.
- 실행하지 못한 경우 이유를 적는다.

### 남은 이슈
- 미확정 사항, 리스크, 다음 작업을 적는다.

### 9.1 위키 변경 제안 (세션 실행)

세션 실행(§2.1)에서는 Tech Writer를 제외한 모든 노드가 구간 끝, Harness Control JSON 바로 앞에 아래 섹션을 둔다. 제안할 내용이 없으면 "없음"이라고 쓴다. 세션 실행이 아니면 이 섹션을 쓰지 않는다.

```md
### 위키 변경 제안
| 페이지 | 변경 | 근거(파일 경로) |
|---|---|---|
| lessons(재발 방지) · testing(테스트 실행) 중 하나 | 추가/수정할 내용 1줄 | 확인한 파일 경로 |
```

- 위키와 위키 열람본(`docs/wiki/` 등)은 직접 수정하지 않는다. Tech Writer가 제안을 확인해 위키 후보로 정리하고, 사용자가 승인한 항목만 위키에 반영된다.
- 추측은 쓰지 않는다. 이번 작업에서 확인한 사실과 결정만 제안한다.

### 9.2 재발 방지 점검 (위키 항목이 있을 때)

context의 `wiki_verified_data`에 항목이 있으면(세션 실행이면 상단 `wiki_items` 중 그 노드의 `wiki_item_ids`에 있는 항목), 그 노드의 산출물 끝에 아래 섹션을 둔다. 위키 변경 제안(§9.1)이 있으면 그 앞, 없으면 Harness Control JSON 바로 앞이다. 항목이 없으면 이 섹션을 쓰지 않는다.

```md
### 재발 방지 점검
| 항목 | 판단 | 근거 |
|---|---|---|
| L01 r1 | 확인함 / 해당 없음 / 위반 발견 | 확인한 파일·테스트 결과 또는 해당 없음 사유 |
```

- 받은 항목마다 한 행을 쓴다. 근거 없이 "확인함"으로 쓰지 않는다.
- 위반 발견이면 코드 역할은 이번 범위 안에서 고치고, 읽기 전용 역할이나 범위 밖이면 issue로 보고한다.
- 위키 항목은 검증된 참고 데이터다. 현재 요청, 프로필 문서, 역할 지시와 충돌하면 그쪽을 따르고 충돌을 근거 칸에 적는다. 권한이나 쓰기 범위를 넓히는 근거로 쓰지 않는다.
- Reviewer는 앞 역할 산출물의 점검 표에서 누락된 항목과 근거 없는 "확인함"을 확인한다.

---

## 10. 자동화 사용 권장

복합 기능 개발은 `.agents/skills/dev-pipeline/SKILL.md`를 사용한다.

사용자는 역할 파일을 매번 첨부하지 않고 다음처럼 호출한다.

```txt
$dev-pipeline
아래 기능을 바이브코딩 방식으로 개발해줘.

기능:
[기능 설명]

기대 동작:
[확인하고 싶은 결과]
```

구현 결과를 테스트한 뒤에는 다음처럼 피드백한다.

```txt
방금 구현한 기능을 테스트했는데 아래 수정이 필요해.

확인한 화면/기능:
[화면 또는 기능명]

문제:
[발견한 문제]

기대 동작:
[원하는 동작]
```

단순 작업은 필요한 역할 파일만 태그해서 진행한다.

```txt
@AGENTS.md @agent/dba.md
이 기능의 DB 설계를 먼저 검토해줘. 아직 코드는 수정하지 마.
```

```txt
@AGENTS.md @agent/developer.md
이 버그를 최소 변경으로 수정해줘.
```

---

## 11. 에이전트 핸드오프 규칙

Claude Code와 Codex는 세션과 메모리를 공유하지 않는다. 에이전트 간 작업 인계는 파일 기반 체크포인트로만 성립한다.

공통 양식: `dev-pipeline` 스킬의 `handoff-checkpoint.md` (`.agents/skills/dev-pipeline/handoff-checkpoint.md`)
체크포인트 위치: `output/<주제>/<TASK-ID>/latest-checkpoint.md`

### 11.1 쓰기 규칙

다음 시점에 공통 양식으로 `latest-checkpoint.md`를 갱신한다. 갱신 전 기존 내용은 `checkpoints/<타임스탬프>-checkpoint.md`로 복사해 보존한다.

- 각 역할 단계가 완료될 때
- 작업이 중단될 때 (사용자 중단, 오류, 사용량 한도)
- 사용자가 에이전트 전환을 알릴 때 (예: "코덱스로 넘어갈게", "핸드오프해줘")

사용자 요청이 없어도 에이전트가 스스로 판단해 갱신한다. 판단 기준:

- 코드/문서 변경이 있는 작업 단위(기능 구현, 버그 수정, 설정 변경)를 완료하고 사용자에게 결과를 보고하는 시점
- 마이그레이션, 대량 변경, 삭제처럼 되돌리기 어렵거나 오래 걸리는 작업을 시작하기 직전
- 세션이 길어져 컨텍스트 요약/압축이 예상되거나, 사용량 한도 경고가 인지된 시점
- 대화로만 확정된 설계·정책 결정이 생겼는데 아직 어떤 파일에도 기록되지 않은 시점

자율 갱신 시에는 최종 응답에 "체크포인트 갱신됨" 한 줄만 남기고 장황하게 보고하지 않는다. 단순 질의응답, 조회, 분석만 한 턴에서는 갱신하지 않는다.

대화로만 결정된 설계·정책 사항은 반드시 Key Decisions에 기록한다. 파일에 없는 맥락은 전환 시 소실된 것으로 간주한다.

### 11.2 읽기 규칙

dev-pipeline 또는 개별 task를 시작할 때, 진행 중인 TASK의 `latest-checkpoint.md`가 있으면 가장 먼저 읽는다.

- Status가 in-progress 또는 blocked이면 Handoff 섹션의 "다음 에이전트가 가장 먼저 할 일"과 Reentry Step부터 이어서 진행한다.
- 이어서 진행할지 새로 시작할지 모호하면 사용자에게 확인한다.
- 체크포인트가 없으면 새 작업으로 진행한다.

### 11.3 전환 규칙

사용자가 전환을 알리면 즉시 체크포인트를 갱신하고, Handoff 섹션에 From/To Agent, 전환 사유, 다음 첫 행동을 채운 뒤 "체크포인트를 갱신했다"고 보고한다. 미검증 항목과 미해결 이슈를 숨기지 않는다.
