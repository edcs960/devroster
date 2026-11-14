# 실행·이관·복원 가이드

아래 `devroster`는 설치된 명령 또는 `.venv/bin/python -m agent_dev.fa`를 뜻한다(`fa`는 호환 별칭). 실행 경로는 v2(승인 snapshot + SQLite) 하나다. 레거시 JSON 파이프라인은 2026-10-29에 제거됐고([ADR 0003](adr/0003-v2-primary-path.md)), 아래 `refactor migrate/compare`는 남아 있는 레거시 자료를 읽어 가져오는 용도다.

## 레거시 자료 가져오기

```bash
devroster refactor migrate --dry-run --json
devroster refactor migrate --json
devroster refactor compare --json
devroster refactor rollback-drill --json
```

- **dry-run**: DB·output을 생성하지 않는다. 실제 import는 `.agent-dev/state.db`와 pre-import backup/manifest를 생성한다.
- **경로**: 기본 DB/output/worktree 위치는 workspace root 기준이다. `--root`는 engine configuration/data root, `--db`와 `--data`는 별도 경로를 지정한다.
- **비교**: comparator가 zero_diff=false이면 execute/read promotion을 하지 않는다. canonical v2 task 산출물은 legacy 비교에서 제외한다.
- **보존**: 원 backlog와 metadata는 수정하지 않는다. 비밀값은 제거하고 원본 구조·의존성·상태 projection 및 산출물 digest를 보존한다.

## 새 프로젝트와 승인 실행

### Profile 작성

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

- Runner authenticated=true는 실제 CLI 로그인을 대신하지 않는다.
- command의 credential 참조만 profile에 두고 token을 JSON에 적지 않는다.
- ambient MCP는 신규 runner에서 제한한다.
- Claude의 기본 tools는 read이며 code.write phase에서 Edit/Write만 추가한다.

### 설계·계획 등록

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

### 실행 조건

- 설계/계획 승인과 immutable snapshot이 있어야 실행한다.
- capacity cooldown 중에는 waiting_for_capacity이며, cooldown 이후 새 request ID로 `devroster task resume`한다.
- 같은 request ID는 기존 결과를 반환한다.
- 동일 ID의 입력 파일 내용이 바뀌면 거부한다.

## 게시와 완료

```bash
devroster refactor configure publish --json
devroster task publish V2-TASK-001 source --json --request-id publish-001
devroster task review V2-TASK-001 --json
devroster task complete V2-TASK-001 PUBLISH-RECORD-ID --json
```

### 게시

- publish는 task worktree와 approved allowed_paths에 한정한다.
- 검증 후 source 변경은 게시를 차단한다.
- 커밋 메시지와 PR 제목·본문은 프로젝트 설정의 작업 언어(`output_language`, 기본 한국어)로 개발 역할 AI가 쓴 `commit_message`를 쓴다.
- PR 본문에는 그 설명 뒤에 마지막 검증 결과와 요구사항 ID가 붙는다.

### 완료 방식

완료 방식은 프로젝트 설정의 `completion_mode`다.

| 값 | 동작 |
|---|---|
| **`local`(기본)** | publish가 task 브랜치에 커밋하고, review가 HEAD 일치와 깨끗한 작업 트리를 확인하며, complete가 기본 브랜치를 빠른 병합(fast-forward)한다. push·PR·CI는 쓰지 않는다 |
| **`github`** | PR과 CI가 필요하다. PR 생성은 완료가 아니다. 모든 publish_required repository의 명시된 CI check가 SUCCESS이고, PR head가 기록 SHA와 같아야 한다 |

### 드리프트 검사와 게시

드리프트 검사를 검증·게시에 연결할지는 프로젝트 설정의 `drift_gate`다. 저장한 뒤 승인하는 작업부터 적용된다.

| 값 | 동작 |
|---|---|
| **`off`(기본)** | 변경 검토 화면에서 직접 실행할 때만 검사한다. 게시와 무관하다 |
| **`advise`** | 검증이 통과하면 검사를 한 번 자동 실행한다(AI 호출 1회). 결과는 검토 화면에 보이고 게시는 막지 않는다 |
| **`block`** | 자동 실행하고, 검증한 코드의 검사가 끝나 심각 지적이 모두 처리(반영·위험 수용·반박)될 때까지 게시를 거부한다 |

- 검사는 실행한 시점의 코드에만 유효하다. 코드를 고쳐 다시 검증하면 검사도 다시 실행된다.
- 검사가 실패하면(AI 도구 로그인·키 문제 등) 검증 결과는 그대로이고, `block`에서는 검토 화면에서 다시 실행해 통과시켜야 게시할 수 있다.
- 요구사항이 연결되지 않은 작업(Studio 인계가 아닌 작업)에는 적용하지 않는다.

### 검토 AI 성적표와 검토 비용

- 사용량 화면(`/usage`)의 '검토 AI 성적표'는 독립 검토와 드리프트 검사에서 각 AI가 낸 지적을 사용자가 어떻게 처리했는지 센다. 반영·위험 수용은 맞음, 반박은 틀림이다. 적중률은 처리한 지적이 5건 이상일 때만 표시한다. 지적을 처리하지 않으면 성적이 쌓이지 않는다.
- 이유를 적어 수용·반박한 지적은 같은 Studio 프로젝트의 다음 독립 검토, 같은 작업의 다음 드리프트 검사에 전달되어 같은 지적이 다시 열리는 것을 줄인다. 이유는 AI 도구에 전송되므로 비밀값을 적지 않는다(등록된 비밀값은 드리프트 검사에서 가려진다).
- 독립 검토의 '검토 깊이'로 모델 계열 수(1~3)를 고른다. AI 호출 수는 최대 관점 수 × 계열 수다. 기본은 3이고, 줄이면 명세 검토 적중률이 확인된 도구부터 쓴다.

### 작업 폴더 정리

작업을 **완료**하면 그 작업의 작업 폴더(워크트리)와 작업 브랜치를 아래 규칙으로 함께 정리한다.

- **남기기**: `devroster task complete … --keep-worktree`, 작업실에서는 "완료 후에도 작업 폴더 남기기".
- **대상 확인**: 취소한 작업이나 남겨 둔 폴더는 `devroster worktree list`로 정리 대상과 이유를 본다.
- **지우기**: `devroster worktree prune --apply`로 지운다(`--task ID`로 한 작업만).
- **지우는 조건**: 완료·취소된 작업 중 작업이 기준 브랜치에 들어갔거나 GitHub에서 병합이 확인된(squash 포함) 것만 지우고, 커밋하지 않은 변경이나 병합되지 않은 커밋이 있으면 남긴다.
- **함께 지우는 것**: 작업 브랜치와 그 작업의 중단 기준선(`.attempts`)도 함께 지운다.
- **로그 보관**: 실시간 로그는 지우지 않고 작업 폴더 밖(`<실행 데이터 폴더>/worktrees/.logs/<작업 ID>/`)으로 옮겨 정리한 때부터 30일 동안 더 보관하며, 그동안 작업실에서 그대로 볼 수 있다.
- **웹에서**: **설정 → 공통 설정 → 작업 폴더 정리**에서 **정리 대상 확인**으로 같은 목록을 보고, **정리 실행**을 누르면 확인 대화상자를 거쳐 미리보기에 나온 폴더만 지운다(미리보기 뒤에 새로 끝난 작업은 다시 확인해야 지운다).

### 중단된 단계 처리

- AI 단계가 도중에 강제 종료되면 다음 실행 전에 그 단계가 남긴 변경을 역할 권한과 대조한다.
- 권한 안이면 이어서 실행하고, 권한 밖이면 작업 폴더의 `.attempts`에 복사한 뒤 `INTERRUPTED_CHANGES`로 멈춘다.
- 이어서 실행하면 그 파일들만 단계 시작 시점으로 되돌리고 다시 실행한다.

### 범위 밖 변경으로 차단된 단계 처리

- AI 단계가 역할이 바꿀 수 없는 파일(수정 허용 경로 밖, 다른 모듈, 읽기 전용 역할의 변경)을 바꾸면 `POLICY_VIOLATION`으로 멈추고, 차단 원인에 그 파일과 수정 허용 경로가 나온다.
- 멈춘 동안 그 변경은 작업 폴더에 그대로 남아 있어 내용을 확인할 수 있다.
- 이어서 실행하면 그 파일들만 작업 폴더의 `.attempts/<시도 ID>-refused`에 복사하고, 그 단계가 시작한 시점으로 되돌린 뒤 단계를 다시 실행한다. 역할이 바꿀 수 있는 파일의 변경은 그대로 둔다.
- 그 파일이 필요하면 프로젝트 설정의 수정 허용 경로에 추가하고 계획을 새로 승인한다. 이미 승인된 작업의 범위는 바뀌지 않는다.
- 되돌릴 기준이 없거나 되돌리기에 실패하면 다시 멈추고, 직접 되돌린 뒤 이어서 실행하라고 안내한다.

### 완료 처리 규칙

- 사용자가 실제 PR을 merge하고 complete를 실행해야 completed가 된다.
- completed 직후 프로젝트에 Graphify 그래프(`graphify-out/graph.json`)가 있으면 `graphify update`로 코드 그래프를 갱신하고 결과를 `code_graph`로 남긴다(실패해도 완료는 유지).
- SKIPPED/누락/FAILURE는 required check의 성공으로 인정하지 않는다.

## Feedback과 지식

```bash
devroster feedback add V2-TASK-001 /tmp/feedback.md --json
devroster feedback plan FEEDBACK-ID FB-PLAN DESIGN-001 1 --json
devroster knowledge candidate PRJ-001 docs/verified.md COMMIT-SHA --json
devroster knowledge confirm CANDIDATE-ID VERIFICATION-RUN-ID --json
devroster knowledge search PRJ-001 fact --json
```

- Feedback에는 별도 task/plan 승인이 필요하며 원 task를 ready/running으로 되돌리지 않는다.
- confirmed knowledge만 project 범위에서 조회하고 committed bytes/digest와 passing verification 근거를 확인한다.

## 외부 도구 연결

```bash
devroster tool register /tmp/tool-connection.json --json
devroster tool grant CONNECTION-ID design.write FILE-KEY V2-TASK-001 designer --json
```

- ToolConnection은 enabled/authenticated/healthy/capabilities/allowlist/credential_ref를 등록한다.
- privileged design.write/security.active_scan/github.write grant는 approved task/phase/scope에 묶이며 5분·일회 소비다.
- 실제 연동은 `adapters/external_tools.py`의 bounded operation 계약을 사용한다.
- 기본 read-only이고 live scanner/브라우저 도구 설치는 자동으로 하지 않는다.

## 알림

```bash
devroster notification drain PRJ-001 --json
```

- Notification은 profile의 notification.connection_id/channel_id 및 notification.send 등록 scope가 있어야 한다.
- `devroster notification drain ... --apply`는 **실제 전송**이다. 기본은 preview, 실행은 별도 worker에서 수행한다.
- outbox는 at-least-once이며 3회 실패 후 dead다.
- Discord nonce는 최근 몇 분의 중복 억제만 지원한다: [Discord message API](https://docs.discord.com/developers/resources/message#create-message).

## 롤백·백업·복원·보존·릴리스 gate

```bash
devroster refactor export /safe/backup/state.db --json
devroster refactor rollback --json
devroster refactor restore /safe/backup/state.db /safe/restored/state.db --json
devroster refactor retention --json
devroster refactor observe-release RELEASE-TAG --json
devroster refactor configure legacy-readonly --json
```

- **export**: DB+SHA manifest를 저장한다. **output artifact 파일과 runtime 설정은 별도로 백업**한다.
- **rollback**: flags off, active lease token 무효화, running attempt interrupted와 active task blocked를 기록하며 DB를 보존한다. 신규 provider supervisor가 lease loss를 감지한다.
- **restore**: existing destination을 덮어쓰지 않고 manifest hash/integrity를 확인한다. 원 경로 복귀는 worker 중단·검증 후 수행한다.
- **retention**: 기본 90일, 최소 30일. completed/cancelled task의 오래된 immutable artifact만 대상이며 active lease·범위·digest를 확인한다. `--apply` 전에 목록을 검토한다. canonical output 문서와 event history는 보존한다.
- **릴리스 gate**: `observe-release`와 `configure legacy-readonly`는 레거시 제거 전 전환 gate용으로 남은 명령이다. 레거시 쓰기 경로가 없어진 지금은 필요하지 않다(ADR 0003 후속 결정).
