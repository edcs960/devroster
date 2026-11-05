# 13 Security Phase

## 목적

이번 task 변경분을 인증/인가/입력검증/민감정보/설정 관점에서 점검하고 심각도별 수정 지시를 남긴다.

## 적용 역할

- `AGENTS.md`
- `agent/security.md`
- Developer/QA/Test Automation phase 결과

## 입력

- 변경 파일 목록
- 인증/권한 관련 코드 (backend security config, controller, 프론트 라우팅 가드)
- 환경변수/설정 파일 사용 방식

## 수행 절차

1. 변경 파일 중 인증/인가/입력 처리 경로를 우선 검토한다.
2. SQL injection, XSS, IDOR, CSRF, 시크릿 하드코딩, 과다 응답 노출을 점검한다.
3. 코드를 수정하지 않는다. 발견 사항과 수정 지시만 작성한다.
4. Critical/High 발견 시 handoff_status를 issue로, owning_agent_id를 해당 developer로 설정한다.

## 산출물 형식

```md
# Security 결과

## 1. 점검 범위
- 검토 파일:

## 2. 발견 사항
| 심각도 | 위치 | 문제 | 재현 시나리오 | 수정 지시 |
|---|---|---|---|---|

## 3. 점검하지 못한 영역
- 항목과 사유

## 4. 판정
- 배포 가능 / 수정 후 재점검 필요
```

마지막에 Harness Control JSON block을 포함한다.
