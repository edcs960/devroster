# 실행·이관·복원 가이드

아래 `devroster`는 설치된 명령 또는 `.venv/bin/python -m agent_dev.fa`를 뜻한다(`fa`는 호환 별칭). 실행 경로는 v2(승인 snapshot + SQLite) 하나다. 레거시 JSON 파이프라인은 2026-10-29에 제거됐고([ADR 0003](adr/0003-v2-primary-path.md)), 아래 `refactor migrate/compare`는 남아 있는 레거시 자료를 읽어 가져오는 용도다.

## 레거시 자료 가져오기

```bash
devroster refactor migrate --dry-run --json
devroster refactor migrate --json
devroster refactor compare --json
devroster refactor rollback-drill --json
```

- dry-run은 DB·output을 생성하지 않는다. 실제 import는 `.agent-dev/state.db`와 pre-import backup/manifest를 생성한다.
- 기본 DB/output/worktree 위치는 workspace root 기준이다. `--root`는 engine configuration/data root, `--db`와 `--data`는 별도 경로를 지정한다.
- comparator가 zero_diff=false이면 execute/read promotion을 하지 않는다. canonical v2 task 산출물은 legacy 비교에서 제외한다.
- 원 backlog와 metadata는 수정하지 않는다. 비밀값은 제거하고 원본 구조·의존성·상태 projection 및 산출물 digest를 보존한다.

## 새 프로젝트와 승인 실행

Profile 예 (`/tmp/profile.json`, 실제 repo/검증 명령으로 수정):

```json
{
  "repositories": {
    "source": {
      "path": "/absolute/path/to/repository",
      "allowed_paths": ["src", "tests"],
      "required_ci_checks": ["unit"],
      "publish_required": true
    }
  },
  "phases": [{"id": "developer", "capabilities": ["code.read", "code.write"]}],
  "role_context": {"developer": "승인된 scope와 허용 경로에서 구현하고 Harness Control JSON을 반환한다."},
  "runners": [{"id": "local-code", "provider": "codex_cli", "enabled": true,
               "authenticated": true, "capabilities": ["code.read", "code.write"]}],
  "verification": {
    "required_gates": ["unit"],
    "commands": [{"name": "unit", "repository": "source", "command": ["python", "-m", "pytest", "-q"]}]
  }
}
```

Runner authenticated=true는 실제 CLI 로그인을 대신하지 않는다. command의 credential 참조만 profile에 두고 token을 JSON에 적지 않는다. ambient MCP는 신규 runner에서 제한한다. Claude의 기본 tools는 read이며 code.write phase에서 Edit/Write만 추가한다.

설계 Markdown에는 Scope/범위, Acceptance/수용 기준, Constraints/제약을 명시한다. Plan은 배열이다:

```json
[{"id":"V2-TASK-001","title":"구현","system_name":"My Product","depends_on":[]}]
```

```bash
devroster project register PRJ-001 /tmp/profile.json --json --request-id project-001
devroster design refine PRJ-001 DESIGN-001 /tmp/design.md --json
devroster design approve DESIGN-001 1 --json
devroster plan create PRJ-001 PLAN-001 DESIGN-001 1 /tmp/tasks.json --json
devroster plan approve PLAN-001 --json
devroster refactor configure execute --json
devroster task run V2-TASK-001 --json --request-id run-001
devroster task verify V2-TASK-001 --json --request-id verify-001
devroster task checkpoint V2-TASK-001 --json
devroster task publish V2-TASK-001 source --dry-run --json
```

설계/계획 승인과 immutable snapshot이 있어야 실행한다. capacity cooldown 중에는 waiting_for_capacity이며, cooldown 이후 새 request ID로 `devroster task resume`한다. 같은 request ID는 기존 결과를 반환한다. 동일 ID의 입력 파일 내용이 바뀌면 거부한다.

## 게시와 완료

```bash
devroster refactor configure publish --json
devroster task publish V2-TASK-001 source --json --request-id publish-001
devroster task review V2-TASK-001 --json
devroster task complete V2-TASK-001 PUBLISH-RECORD-ID --json
```

- publish는 task worktree와 approved allowed_paths에 한정한다. 검증 후 source 변경은 게시를 차단한다.
- 커밋 메시지와 PR 제목·본문은 프로젝트 설정의 작업 언어(`output_language`, 기본 한국어)로 개발 역할 AI가 쓴 `commit_message`를 쓴다. PR 본문에는 그 설명 뒤에 마지막 검증 결과와 요구사항 ID가 붙는다.
- 완료 방식은 프로젝트 설정의 `completion_mode`다. 기본 `local`은 publish가 task 브랜치에 커밋하고, review가 HEAD 일치와 깨끗한 작업 트리를 확인하며, complete가 기본 브랜치를 빠른 병합(fast-forward)한다. push·PR·CI는 쓰지 않는다.
- `github`를 고르면 아래처럼 PR과 CI가 필요하다. PR 생성은 완료가 아니다. 모든 publish_required repository의 명시된 CI check가 SUCCESS이고, PR head가 기록 SHA와 같아야 한다.
- 끝난 작업의 작업 폴더(워크트리)는 자동으로 지우지 않는다. `devroster worktree list`로 정리 대상과 이유를 보고, `devroster worktree prune --apply`로 지운다(`--task ID`로 한 작업만). 완료·취소된 작업 중 작업이 기준 브랜치에 들어갔거나 GitHub에서 병합이 확인된(squash 포함) 것만 지우고, 커밋하지 않은 변경이나 병합되지 않은 커밋이 있으면 남긴다. 작업 브랜치와 그 작업의 중단 기준선(`.attempts`)도 함께 지운다. 웹에서는 **설정 → 공통 설정 → 작업 폴더 정리**에서 **정리 대상 확인**으로 같은 목록을 보고, **정리 실행**을 누르면 확인 대화상자를 거쳐 미리보기에 나온 폴더만 지운다(미리보기 뒤에 새로 끝난 작업은 다시 확인해야 지운다).
- AI 단계가 도중에 강제 종료되면 다음 실행 전에 그 단계가 남긴 변경을 역할 권한과 대조한다. 권한 안이면 이어서 실행하고, 권한 밖이면 작업 폴더의 `.attempts`에 복사한 뒤 `INTERRUPTED_CHANGES`로 멈춘다. 이어서 실행하면 그 파일들만 단계 시작 시점으로 되돌리고 다시 실행한다.
- 사용자가 실제 PR을 merge하고 complete를 실행해야 completed가 된다. completed 직후 프로젝트에 Graphify 그래프(`graphify-out/graph.json`)가 있으면 `graphify update`로 코드 그래프를 갱신하고 결과를 `code_graph`로 남긴다(실패해도 완료는 유지). SKIPPED/누락/FAILURE는 required check의 성공으로 인정하지 않는다.

## Feedback, knowledge, 외부 도구, 알림

```bash
devroster feedback add V2-TASK-001 /tmp/feedback.md --json
devroster feedback plan FEEDBACK-ID FB-PLAN DESIGN-001 1 --json
devroster knowledge candidate PRJ-001 docs/verified.md COMMIT-SHA --json
devroster knowledge confirm CANDIDATE-ID VERIFICATION-RUN-ID --json
devroster knowledge search PRJ-001 fact --json
devroster tool register /tmp/tool-connection.json --json
devroster tool grant CONNECTION-ID design.write FILE-KEY V2-TASK-001 designer --json
devroster notification drain PRJ-001 --json
```

Feedback에는 별도 task/plan 승인이 필요하며 원 task를 ready/running으로 되돌리지 않는다. confirmed knowledge만 project 범위에서 조회하고 committed bytes/digest와 passing verification 근거를 확인한다.

ToolConnection은 enabled/authenticated/healthy/capabilities/allowlist/credential_ref를 등록한다. privileged design.write/security.active_scan/github.write grant는 approved task/phase/scope에 묶이며 5분·일회 소비다. 실제 연동은 `adapters/external_tools.py`의 bounded operation 계약을 사용한다. 기본 read-only이고 live scanner/브라우저 도구 설치는 자동으로 하지 않는다.

Notification은 profile의 notification.connection_id/channel_id 및 notification.send 등록 scope가 있어야 한다. `devroster notification drain ... --apply`는 **실제 전송**이다. 기본은 preview, 실행은 별도 worker에서 수행한다. outbox는 at-least-once이며 3회 실패 후 dead다. Discord nonce는 최근 몇 분의 중복 억제만 지원한다: [Discord message API](https://docs.discord.com/developers/resources/message#create-message).

## 롤백·백업·복원·보존·릴리스 gate

```bash
devroster refactor export /safe/backup/state.db --json
devroster refactor rollback --json
devroster refactor restore /safe/backup/state.db /safe/restored/state.db --json
devroster refactor retention --json
devroster refactor observe-release RELEASE-TAG --json
devroster refactor configure legacy-readonly --json
```

- export는 DB+SHA manifest를 저장한다. **output artifact 파일과 runtime 설정은 별도로 백업**한다.
- rollback은 flags off, active lease token 무효화, running attempt interrupted와 active task blocked를 기록하며 DB를 보존한다. 신규 provider supervisor가 lease loss를 감지한다.
- restore는 existing destination을 덮어쓰지 않고 manifest hash/integrity를 확인한다. 원 경로 복귀는 worker 중단·검증 후 수행한다.
- retention은 기본 90일, 최소 30일. completed/cancelled task의 오래된 immutable artifact만 대상이며 active lease·범위·digest를 확인한다. `--apply` 전에 목록을 검토한다. canonical output 문서와 event history는 보존한다.
- `observe-release`와 `configure legacy-readonly`는 레거시 제거 전 전환 gate용으로 남은 명령이다. 레거시 쓰기 경로가 없어진 지금은 필요하지 않다(ADR 0003 후속 결정).
