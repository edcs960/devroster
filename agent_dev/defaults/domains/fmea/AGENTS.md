## 12. 도메인 규칙: AIAG-VDA FMEA

이 프로젝트는 AIAG-VDA FMEA 시스템이다. 아래 규칙은 §1~§11에 더해 적용한다.

### 12.1 FMEA Expert 역할

- 역할 파일: `agent/fmea-expert.md`. 코드를 수정하지 않는 단계다(§3.1).
- 기본 개발 파이프라인에서 Planner 바로 다음에 둔다. FMEA Expert는 방법론 기준을 정해 Architect·DBA 설계의 입력으로 넘긴다(ADR 0002). 레거시 dev-pipeline은 `pipeline.json` 순서(FMEA Expert가 Designer 다음)를 따른다.
- 세션 실행에서는 분석 `analysis` 세션(Planner, FMEA Expert, Architect, DBA)에 속한다.
- FMEA 방법론, AIAG 4판, AIAG & VDA 통합판, DFMEA/PFMEA/Control Plan 연계, Function/Failure Chain, AP/RPN 평가, 상용 FMEA 도구 적합성 검증이 관련된 작업은 반드시 FMEA Expert 역할을 포함한다.

### 12.2 피드백 재진입

| 피드백 유형 | 재진입 단계 |
|---|---|
| FMEA 방법론 변경 | FMEA Expert → Architect → Developer → QA |

### 12.3 기술 스택

- Frontend 화면, React, GoJS, 상태관리, UI 테스트: `agent/developer-frontend.md`
- Backend API, Spring Boot, 도메인 로직, 권한, Audit: `agent/developer-backend.md`
