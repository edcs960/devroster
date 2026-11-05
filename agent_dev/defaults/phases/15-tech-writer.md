# 15 Tech Writer Phase

## 목적

완료된 기능의 사용자 가이드, 운영 문서, 변경 요약과 프로필 문서를 정합화한다.

## 적용 역할

- `AGENTS.md`
- `agent/tech-writer.md`
- 모든 이전 phase 결과

## 입력

- task brief와 Planner/Architect 산출물
- Developer 구현 요약과 QA/Security 결과
- 화면 기준 (`Design Reference Context`가 있는 경우)

## 수행 절차

1. 이번 task에서 사용자에게 보이는 변화와 운영에 필요한 변화를 정리한다.
2. `output/<주제>/docs/` 하위에 사용자가이드.md, 운영가이드.md, CHANGELOG.md를 작성/갱신한다.
3. 구현 계약과 어긋난 프로필 문서도 갱신한다.
4. 문서 파일만 수정한다. 프론트/백엔드 코드, DB migration, 실행 설정은 수정하지 않는다.
5. 코드·DB·설정 변경이나 정책 결정이 필요한 불일치는 issue로 라우팅한다.

## 산출물 형식

```md
# Tech Writer 결과

## 1. 작성/갱신한 문서
- 파일 목록

## 2. 변경 요약(CHANGELOG 반영 내용)
- 추가/변경/제거

## 3. 확인 필요 항목
- 추정으로 작성해 사용자 확인이 필요한 부분
```

마지막에 Harness Control JSON block을 포함한다.
