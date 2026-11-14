# 06 FMEA Expert Phase

## 목적

Planner, Architect, DBA, UX/UI, Designer 산출물이 AIAG & VDA FMEA 통합판, DFMEA/PFMEA 업무 흐름, 실제 FMEA 도구 운용 관점에 맞는지 개발 전에 종합 검토한다.

## 적용 역할

- `AGENTS.md`
- `agent/fmea-expert.md`
- `.agents/skills/dev-pipeline/SKILL.md`

## 입력

- Planner 결과
- Architect 결과
- DBA 결과
- UX/UI 결과
- Designer 결과
- 방법론 참고 문서
- 사용자요구사항정의서, 유스케이스명세서, 엔티티관계모형기술서, 사용자인터페이스설계서, 인터페이스설계서, 정책정의서

## 수행 절차

1. AIAG & VDA 7-Step 기준으로 요구사항, 설계, 화면 흐름을 매핑한다.
2. DFMEA/PFMEA, Structure/Function/Failure/Action 흐름이 설계와 화면에서 일관적인지 검토한다.
3. Function Chain, Failure Chain, AP/RPN, S/O/D, Control Plan 연계가 누락되지 않았는지 확인한다.
4. Revision, 승인, 잠금, Audit, Export 요구가 API/DB/UX 설계에 반영되었는지 점검한다.
5. Developer 단계 전에 반드시 보완해야 할 방법론 리스크와 구현 지시를 전달한다.

## 산출물 형식

```md
# FMEA Expert 결과

## 1. 검토 범위
- 기준:
- 참조 문서:

## 2. 핵심 판단
- ...

## 3. AIAG & VDA 7-Step 반영 여부
| Step | 충족 여부 | 근거 | 보완 필요 |
|---:|---|---|---|
| 1 | Yes/Partial/No | ... | ... |

## 4. 도메인 흐름 검토
| 항목 | 판단 | 근거 | 보완 |
|---|---|---|---|
| Function Chain | ... | ... | ... |
| Failure Chain | ... | ... | ... |
| AP/RPN | ... | ... | ... |
| Control Plan | ... | ... | ... |

## 5. 산출물별 수정 권고
| 산출물 | 수정 필요 | 권고 내용 |
|---|---|---|
| ... | Yes/No | ... |

## 6. 방법론 리스크
| 심각도 | 리스크 | 영향 | 대응 |
|---|---|---|---|
| High/Medium/Low | ... | ... | ... |

## 7. 다음 Phase 전달 사항
- Architect:
- DBA:
- UX/UI:
- Developer:
```

## 완료 기준

- 방법론 위반 또는 누락이 명확히 식별되어 있다.
- 개발/DB/API/UI 중 어느 영역이 보완해야 하는지 드러난다.

## Harness Control JSON

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "FMEA methodology review completed."
}
```
