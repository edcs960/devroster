# 05 Designer Phase

## 목적

UX/UI 설계를 시각적 계층, 레이아웃, 컴포넌트 스타일, 반응형 기준으로 구체화한다.

## 실행 조건

- `workstream:frontend`
- 또는 화면 스타일, 디자인 시스템, 컴포넌트 표현 변경이 필요한 경우

## 적용 역할

- `AGENTS.md`
- `agent/designer.md`
- 필요 시 `agent/design.md`
- 필요 시 `AGENTS.md` 6절 디자인 참고 자료 규칙

## 입력

- UX/UI 결과
- 사용자인터페이스설계서
- 기존 공통 UI 컴포넌트, 디자인 토큰, Tailwind 설정, 스타일 규칙
- `Design Reference Context`가 있으면 `/references`에서 프로젝트에 적용한 디자인 참고 자료(Stitch/Figma)

## 수행 절차

1. 기존 디자인 시스템과 공통 컴포넌트를 확인한다.
2. 화면의 시각적 계층과 레이아웃을 정의한다.
3. 컬러, 타이포그래피, spacing, 아이콘 사용 기준을 정리한다.
4. `Design Reference Context`가 있으면 참고 화면을 읽기 전용으로 확인하고, 요구사항 변경 또는 사용자인터페이스설계서와 불일치가 있으면 구현 보정 사항과 구현 기준을 정리한다. Stitch 원본은 수정하지 않는다.
5. Button, Input, Table/List, Card, Modal 등 컴포넌트 스타일을 제안한다.
6. 반응형과 접근성 리스크를 Developer에게 전달한다.

## 산출물 형식

```md
# Designer 결과

## 1. 디자인 방향 요약
- ...

## 2. 시각적 계층
| 요소 | 우선순위 | 표현 방식 | 비고 |
|---|---|---|---|
| ... | ... | ... | ... |

## 3. 레이아웃/여백/정렬
- Desktop:
- Tablet:
- Mobile:

## 4. 컬러/상태 표현
| 상태 | 표현 방식 | 주의사항 |
|---|---|---|
| Success | ... | ... |
| Error | ... | ... |

## 5. 컴포넌트 스타일 가이드
### Button
- ...

### Input
- ...

### Table/List
- ...

### Card
- ...

### Modal
- ...

## 6. 디자인 시스템 체크포인트
- 재사용 가능한 컴포넌트:
- 새로 필요한 변형:
- 기존 스타일과 충돌 가능성:

## 7. 디자인 참고 자료 확인 결과

`Design Reference Context`가 있는 경우 이 섹션은 필수다.

- 사용한 참고 자료 ID와 출처(Stitch/Figma)
- 확인한 화면/노드
- 원본 수정 여부: 항상 `수정하지 않음 (read-only)`
- 디자인 시스템 적용/변경 여부
- Frontend Developer에게 전달할 레이아웃, 색상, 타이포, 상태 표현 기준
- 사용 여부: Yes/No
- 확인한 화면/프로토타입:
- 반영한 시각 방향:
- 기존 디자인 시스템에 맞게 조정한 내용:
- 연결 실패 또는 미사용 이유:

## 8. 다음 Phase 전달 사항
- Frontend Developer:
- QA:
- Reviewer:
```

## 완료 기준

- 기존 디자인 시스템과 충돌하지 않는다.
- Developer가 실제 스타일 구현에 참고할 수 있다.

## Harness Control JSON

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "Designer phase completed."
}
```
