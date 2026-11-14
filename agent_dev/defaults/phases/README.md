# Dev Phase Guide

이 폴더는 `$dev-pipeline`을 반복적으로 사용할 때 각 phase가 남겨야 할 산출물 기준을 정의한다.

레거시 dev-pipeline의 `pipeline.json`(기본: `agent_dev/defaults/config/pipeline.json`) 단계와 1:1로 맞추며, 실제 태스크 실행 산출물은 `output/<system>/TASK-ID/` 아래에 작성한다. 아래 표의 순서가 레거시 실행 순서다(새 Workflow의 기본 역할 순서는 `AGENTS.md` §2).

## 사용 방법

1. 작업 시작 시 `output/<system>/TASK-ID/00-task-brief.md`로 태스크 범위를 확인한다.
2. 현재 phase에 해당하는 문서를 읽고 산출물 형식에 맞춰 결과를 작성한다.
3. 다음 phase가 바로 이어받을 수 있도록 결정 사항, 근거, 리스크, 전달 항목을 짧고 명확하게 남긴다.
4. 이슈가 있으면 `Harness Control JSON`의 `handoff_status`, `issue_type`, `owning_agent_id`, `reentry_step_id`를 사용해 재진입 단계를 명시한다.

## Phase 목록

| 순서 | Phase | Agent | 파일 | 실행 조건 |
|---|---|---|---|---|
| 1 | `01-planner` | Planner | `01-planner.md` | 항상 |
| 2 | `02-architect` | Architect | `02-architect.md` | 항상 |
| 3 | `03-dba` | DBA | `03-dba.md` | `workstream:db` |
| 4 | `04-ux-ui` | UX/UI | `04-ux-ui.md` | `workstream:frontend` |
| 5 | `05-designer` | Designer | `05-designer.md` | `workstream:frontend` |
| 6 | `06-fmea-expert` | FMEA Expert | `06-fmea-expert.md` | `workstream:fmea` |
| 7 | `07-developer-common` | Developer Common | `07-developer-common.md` | 항상 |
| 8 | `08-frontend-developer` | Frontend Developer | `08-frontend-developer.md` | `workstream:frontend` |
| 9 | `09-backend-developer` | Backend Developer | `09-backend-developer.md` | `workstream:backend` |
| 10 | `10-infra-developer` | Infra Developer | `10-infra-developer.md` | `workstream:infra` |
| 11 | `11-qa` | QA | `11-qa.md` | 항상 |
| 12 | `12-test-automation` | Test Automation Developer | `12-test-automation.md` | 항상 |
| 13 | `13-security` | Security Reviewer | `13-security.md` | 항상 |
| 14 | `14-reviewer` | Reviewer | `14-reviewer.md` | 항상 |
| 15 | `15-tech-writer` | Tech Writer | `15-tech-writer.md` | 항상 |

## 공통 완료 조건

- 참조한 문서와 역할 파일을 명시한다.
- 모르는 내용은 추정으로 표시하고, 사실처럼 단정하지 않는다.
- 다음 phase가 필요한 입력을 찾을 수 있게 `다음 Phase 전달 사항`을 작성한다.
- 검증하지 못한 항목은 `미검증`으로 표시한다.
- 화면 설계, 디자인 탐색, UI 구현 검증이 필요한 경우 `Design Reference Context`를 확인하고, 참고 자료 사용 여부와 결과를 phase 산출물에 남긴다.
- 마지막에 `Harness Control JSON`을 포함한다.

## 디자인 참고 자료 확인 기준

화면 관련 작업에서 프롬프트에 `Design Reference Context`(`/references`에서 프로젝트에 적용한 디자인 참고 자료(Stitch/Figma))가 있으면 UX/UI, Designer, Frontend Developer, QA, Reviewer phase는 해당 참고 자료를 사용하고 참고 자료 ID와 사용 결과를 산출물에 남긴다. 참고 자료가 없으면 사용자인터페이스설계서와 기존 디자인 시스템을 기준으로 진행한다.

| Phase | 사용 기준 |
|---|---|
| 05 UX/UI | 사용자 흐름, 정보 구조, 상태별 화면 아이디어 확인 |
| 06 Designer | 레이아웃, 시각 계층, 컴포넌트 표현 방향 확인 |
| 08 Frontend Developer | 확정된 UX/UI·Designer 산출물과 구현 화면의 차이 확인 |
| 11 QA | 실제 구현 화면이 수용 기준, 상태 표현, 반응형 기준을 만족하는지 확인 |
| 12 Reviewer | 디자인 참고 자료와 구현 결과의 차이가 요구사항 위반인지 의도적 조정인지 검토 |

디자인 참고 자료는 읽기 전용 화면 구현의 시각 기준이며, 최종 판단은 `AGENTS.md`, 역할 파일, 사용자인터페이스설계서, 기존 디자인 시스템, 사용자 요청을 우선한다. 요구사항 변경으로 참고 화면과 차이가 나면 UX/UI 또는 Designer phase에서 구현 보정 사항과 근거를 산출물에 기록한다. Stitch/Figma 원본은 수정하지 않는다.

## Harness Control JSON 기본값

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "Ready for the next role."
}
```
