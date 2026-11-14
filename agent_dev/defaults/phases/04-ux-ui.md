# 04 UX/UI Phase

## 목적

사용자 흐름, 정보 구조, 상호작용, 상태 표현, 접근성 기준을 정의한다.

## 실행 조건

- `workstream:frontend`
- 또는 화면 흐름, 입력, 탐색, 반응형, 접근성 변경이 필요한 경우

## 적용 역할

- `AGENTS.md`
- `agent/ux-ui.md`
- 필요 시 `AGENTS.md` 6절 디자인 참고 자료 규칙

## 입력

- Planner 결과
- Architect 결과
- 사용자인터페이스설계서, 사용자요구사항정의서, 권한정의서
- `Design Reference Context`가 있으면 `/references`에서 프로젝트에 적용한 디자인 참고 자료(Stitch/Figma)

## 수행 절차

1. 사용자 시나리오와 진입 경로를 정리한다.
2. 화면 흐름을 `진입 -> 탐색/입력 -> 검토 -> 실행 -> 피드백 -> 후속 행동`으로 정의한다.
3. Default, Loading, Empty, Error, Success, Disabled, Permission 상태를 설계한다.
4. `Design Reference Context`가 있으면 참고 화면을 읽기 전용으로 확인하고, 사용자인터페이스설계서/요구사항과 맞지 않는 경우 구현 보정 사항을 명시한다. Stitch 원본은 수정하지 않는다.
5. CTA, 마이크로카피, 접근성, 반응형 기준을 정리한다.
6. Designer와 Developer에게 전달할 동작 기준을 작성한다.

## 산출물 형식

````md
# UX/UI 결과

## 1. 사용자 시나리오
- 사용자:
- 목표:
- 진입 경로:
- 완료 조건:

## 2. 화면 흐름
```txt
...
```

## 3. 정보 구조
- Primary:
- Secondary:
- Hidden/Progressive Disclosure:

## 4. 상태별 설계
### Default
- ...

### Loading
- ...

### Empty
- ...

### Error
- ...

### Success
- ...

### Permission
- ...

## 5. CTA/마이크로카피
| 위치 | 문구 | 이유 |
|---|---|---|
| ... | ... | ... |

## 6. 접근성/반응형 체크포인트
- 키보드:
- 스크린리더:
- 색상 대비:
- 모바일:

## 7. 디자인 참고 자료 확인 결과

`Design Reference Context`가 있는 경우 이 섹션은 필수다.

- 사용한 참고 자료 ID와 출처(Stitch/Figma)
- 확인한 화면/노드
- 원본 수정 여부: 항상 `수정하지 않음 (read-only)`
- 사용자인터페이스설계서와의 차이
- Frontend Developer에게 전달할 구현 기준
- 사용 여부: Yes/No
- 확인한 화면/프로토타입:
- 반영한 내용:
- 반영하지 않은 내용과 이유:
- 연결 실패 또는 미사용 이유:

## 8. 다음 Phase 전달 사항
- Designer:
- Frontend Developer:
- QA:
````

## 완료 기준

- 개발자가 UI 동작과 상태를 구현할 수 있다.
- 디자이너가 시각 설계로 이어갈 수 있다.

## Harness Control JSON

```json
{
  "handoff_status": "pass",
  "issue_type": "none",
  "owning_agent_id": "",
  "root_cause": "",
  "reentry_step_id": "",
  "summary": "UX/UI phase completed."
}
```
