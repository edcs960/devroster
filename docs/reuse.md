# 다른 프로젝트에서 사용하기

DevRoster 0.4.0. Python 3.11+와 Git을 사용하는 로컬 개발 프로젝트용이다. 기본 명령은 `devroster`이고 `fa`·`agent-dev`는 같은 명령의 별칭이다. 특정 도메인의 역할 파일이나 폴더 구조를 요구하지 않는다.

## 1. 설치와 초기화

Python 3.11 이상인 interpreter로 실행한다. 새 프로젝트의 소스를 먼저 clone하고 baseline commit이 있어야 task worktree를 만들 수 있다.

```sh
python -m venv .venv
# macOS/Linux
source .venv/bin/activate
# Windows는 .venv/Scripts/activate 사용
python -m pip install /path/to/devroster-0.4.0-py3-none-any.whl
devroster init --root /path/to/my-project --name MyProject --preset python
cd /path/to/my-project
devroster doctor
devroster dashboard
```

소스에서 설치할 때는 `python -m pip install /path/to/devroster`도 가능하다. wheel은 `python -m build --wheel`로 만든다. 기본 설치에는 Python runtime dependency가 없다. OpenAI Responses API legacy provider가 필요하면 `python -m pip install 'devroster[openai]'`를 사용하거나 소스 경로에 `[openai]` extra를 붙인다. Codex/Claude CLI는 별도 설치·인증을 사용한다.

기존 wheel 설치를 업데이트할 때는 같은 버전의 실행 파일도 갱신되도록 다시 설치한다.

```sh
python -m pip install --force-reinstall --no-deps /path/to/devroster-0.4.0-py3-none-any.whl
```

다른 프로젝트의 설정 파일을 복사할 필요가 없다. `init`은 기존 `.agent-dev`를 덮어쓰지 않는다.

처음 설정은 실행·게시가 꺼져 있다. `init --runner claude_cli`로 Claude를 선택할 수 있다. 기본은 Codex이며 내장 planner/developer/qa 3단계로 시작한다. 실제 runner CLI는 별도로 설치·로그인해야 한다.

## 2. 프로젝트 격리와 이동

| 위치 | 내용 |
|---|---|
| `.agent-dev/workspace.json` | format_version=1, project_id, name |
| `.agent-dev/config/runtime.json` | 실행 플래그; 새 workspace는 read=true, execute/publish=false |
| `.agent-dev/config/profile.json` | repository/phase/role/runner/verification 설정 |
| `.agent-dev/starter/` | 수정해서 사용할 design.md/tasks.json |
| `.agent-dev/state.db` | SQLite 상태·승인·attempt·event·evidence 참조 |
| `.agent-dev/worktrees/` | task별 격리 source checkout |
| `output/<시스템명>/<TASK-ID>/` | prompt/output/checkpoint/verification evidence |

하위 폴더에서도 marker를 찾아 같은 workspace를 사용한다. 위치 선택은 명시 `--root` → `AGENT_DEV_CONFIG_ROOT` → 현재 경로의 marker → 기존 source engine 설정 순서다. 설치된 package 폴더에는 프로젝트 데이터를 쓰지 않는다. 실행 전 `devroster doctor --root PROJECT`의 workspace 값을 확인할 수 있다.

Repository profile의 `path`는 프로젝트 기준 상대 경로이며 절대 경로도 명시적으로 지정할 수 있다. 이동 후 새 task를 만들면 새로운 경로에서 worktree를 준비한다. 이미 승인된 snapshot에는 당시의 절대 경로가 들어 있으므로 이동 후 자동 재개하지 않는다.

### 설정 위치를 소스와 분리하기

관리 폴더는 `DEVROSTER_HOME` → 기본 앱 위치의 `bootstrap.json`이 가리키는 폴더 → OS 기본 앱 위치 순으로 정한다. 관리 폴더의 `app.json`은 프로젝트마다 고정 `workspace_id`와 소스·설정·실행 위치를 기록한다. 등록된 매핑은 소스 안에 남은 `.agent-dev/workspace.json`보다 우선한다. 같은 업무 `project_id`를 가진 다른 clone은 다른 폴더이므로 서로의 DB에 연결되지 않는다.

```sh
devroster settings location show --root PROJECT            # 관리 폴더와 프로젝트의 실제 저장 위치
devroster settings location plan DEST --scope workspace --root PROJECT   # 사전 점검(아무것도 쓰지 않음)
devroster settings location apply PLAN_ID --root PROJECT    # 복사 → 검증 → 마지막에 위치 전환
devroster settings location recover PLAN_ID --action resume # 중단된 이전 이어서 완료
devroster settings location recover PLAN_ID --action rollback
devroster settings location plan DEST --scope management    # 관리 폴더 전체(DEVROSTER_HOME 미지정 시)
```

- 외부 설정 폴더에는 `workspace.json`, `config/`, `state.db`, `studio/studio.db`, `roles/`, `cache/`가 있고 비공개 환경값·OAuth는 `<관리 폴더>/credentials/projects/<workspace_id>/`에 0600으로 저장한다. worktree와 output은 소스 쪽에 그대로 남는다.
- 이전 전 점검은 실행 중 작업·lease·Studio 작업·CLI 프로세스, 경로 겹침, 디스크 공간, 승인 snapshot의 worktree 경로를 확인한다. 하나라도 실패하면 실행할 수 없다.
- 실행은 SQLite backup API로 복사하고 테이블별 행 digest·파일 digest·증거 파일·`orchestrator_v2` 플래그·`refactor compare` 결과를 비교한 뒤에만 위치를 바꾼다. 원본은 지우지 않는다.
- 전환 뒤 새 위치에 쓰기가 있었다면 `rollback`은 거부되고 상태가 `rollback_required`가 된다. 이때는 현재 DB를 보존하는 새 이전이 필요하다.
- 관리 폴더를 옮긴 뒤에는 웹·CLI·데스크톱 앱을 모두 다시 연다. Claude CLI는 macOS에서 자격 증명을 Keychain에 둘 수 있으므로 `claude auth status`로 앱 전용 로그인을 다시 확인한다. 저장된 Claude 이어쓰기 세션은 새 CLI 홈에서 새 세션으로 대체될 수 있으며 이전 실패가 아니다.

Git ignore에 `/.agent-dev/`와 해당 generated output 디렉터리를 추가한다. source code와 필요 business AGENTS.md는 일반 Git 관리대로 유지한다. Provider 인증 파일과 환경변수는 프로젝트 간 복사하지 않는다.

## 3. 진단

```sh
devroster doctor --json
devroster doctor --root /path/to/project --check-auth --json
```

기본은 offline/read-only이다. Python/Git, marker/runtime, DB quick_check/FK/schema, source HEAD/branch, ignore 규칙, profile 미반영, 실제 test gate 유무, runner executable/선언, tool 등록/credential 환경 참조, lease/recovery, disk를 확인한다. DB가 없으면 생성하지 않는다.

Codex model check는 project model 또는 inherited global model을 로컬 cache 목록과 비교한다. 목록 부재/모델 불일치는 warning이며 cache 성공도 live access를 보증하지 않는다. CLI 지원 모델을 프로젝트 `runners[].config.model`에 지정하고 새 task를 승인하면 전역 설정을 바꾸지 않아도 된다.

각 check는 pass/warn/fail, message, fix를 반환한다. fail이 있으면 exit1이다. `healthy=true`는 fail이 없다는 뜻이며 warning이나 실제 실행 가능성을 보증하지 않는다. 인증을 검사하지 않은 상태를 성공으로 표시하지 않는다.

`--check-auth`는 최대 10초의 Codex `login status`/Claude `auth status --json`만 실행한다. 계정/토큰 원문을 출력하지 않고 성공 여부만 표시한다. 모델 generation은 수행하지 않는다. Tool connection의 registered auth/health는 선언이며 live service 인증 검증이 아니다.

## 4. Profile과 역할 확장

| 항목 | 사용 방법 |
|---|---|
| repositories | source 경로, narrow allowed_paths, 게시 시 remote/default_branch/required_ci_checks |
| phases | id, capabilities(code.read/code.write), 선택 role_file(내장 역할 이름. 예: `agent/planner.md`) |
| role_context | 내장 역할 본문 뒤에 붙는 실행 계약(하네스 JSON·권한 지시) |
| runners | provider, capabilities, enabled, authenticated, config |
| verification | command argument array, repository, cwd, timeout_seconds, required_gates |

`allowed_paths`는 작성·게시 범위를 좁게 지정한다. 초기 `src/tests`가 맞지 않으면 init 옵션이나 profile을 수정한다. `git diff --check`는 공백 검사이며 실제 테스트가 아니다. 프로젝트의 test/lint/build를 필수 gate로 추가한다. shell 문자열 대신 배열을 사용한다.

예: Python 프로젝트의 검증 설정 부분.

```json
{
  "commands": [{"name":"unit","repository":"source","command":["python","-m","pytest","-q"],"required":true,"timeout_seconds":900}],
  "required_gates": ["unit"]
}
```

Provider 로그인 확인 후 의도한 runner의 authenticated 선언을 true로 바꾸고 적용한다. 기존 approved snapshot은 바뀌지 않는다.

```sh
devroster project profile MyProject .agent-dev/config/profile.json --json
```

초기화는 project를 이미 등록하므로 수정에는 `profile`을 쓴다. `project register`는 같은 DB에 별도 project를 처음 등록하는 명령이다. 설계 승인 전 profile을 검토한다. 승인 시 내장 룰·역할 텍스트가 snapshot에 고정된다.

### 내장 룰·역할·스킬

운영 룰(`AGENTS.md`), 역할 15종, phase 지침, `dev-pipeline` 스킬(핸드오프 체크포인트 양식 포함), 프롬프트, 기본 `agents.json`·`pipeline.json`·프로젝트 프로필·문서 템플릿은 패키지(`agent_dev/defaults/`)에 내장되어 모든 설치에서 동일하게 적용된다.

- 프로젝트 폴더의 `AGENTS.md`, `agent/*.md`, `.agents/skills/` 파일은 읽지 않는다. 룰 변경은 패키지 업데이트로만 반영된다.
- 프로젝트별 기준 문서 경로는 프로필의 `context_files`, `role_required_context`가 지정한다. workspace `config/project-profiles/`·`config/templates/`에 같은 이름이 있으면 그것을, 없으면 내장본을 쓴다.
### 프로젝트 역할 구성

설정 화면 **3 개발 역할**에서 프로젝트별 역할과 순서를 정한다. 저장하면 `profile["roles"]`(`domain`, `order`, `custom`)와 `project-roles` Workflow가 함께 갱신된다.

| 구분 | 규칙 |
|---|---|
| 기본 역할 14종 | 내장 AGENTS.md 순서. 순서만 변경, 삭제 불가, 지시문 수정 불가 |
| 선택 역할 (FMEA Expert) | 도메인 프로필 `fmea`면 기본 포함(항상 실행), `generic`이면 미포함. 직접 추가·삭제 가능. `generic`에서 추가하면 FMEA 위험이 감지된 작업에서만 실행 |
| 추가 역할 (최대 20) | 이름·지시문 직접 작성. 권한(읽기 전용/문서 작성/코드 수정)은 생성 시 고정 |

역할을 바꾸지 않고 저장하면 `/workflows`에서 적용한 Workflow를 유지한다. FMEA 관련 작업인데 FMEA Expert가 빠져 있으면 실행 전 차단된다.

- 이 저장소 개발자는 `agent_dev/defaults/`를 수정한 뒤 `python scripts/sync_defaults.py`로 상위 폴더 개발용 사본을 갱신한다. `--check`는 차이만 보고한다.

## 5. 첫 작업 실행

starter 파일을 실제 요구사항으로 교체한 후 아래 순서로 진행한다. project_id는 marker/doctor 결과를 사용한다.

```sh
devroster design refine MyProject DESIGN-001 .agent-dev/starter/design.md
devroster design approve DESIGN-001 1
devroster plan create MyProject PLAN-001 DESIGN-001 1 .agent-dev/starter/tasks.json
devroster plan approve PLAN-001
devroster refactor configure execute
devroster task run TASK-001
devroster task verify TASK-001
devroster task explain TASK-001
```

승인된 task만 실행하며 사용자 source checkout은 보존된다. Native workspace에서 `devroster run TASK-ID`, `devroster verify TASK-ID`, `devroster resume TASK-ID`, `devroster status`도 지원한다. Publish는 별도 플래그·승인·검증·repo/PR/CI 정책이 필요하다. 원격 게시를 초기화 과정에서 자동 수행하지 않는다.

## 6. 진행 뷰

```sh
devroster task list --limit 100 --json
devroster task explain TASK-001 --limit 100 --json
devroster dashboard --port 8766
```

대시보드는 `http://127.0.0.1:8766`에서 전체/진행/확인 필요/완료 건수, 클릭 가능한 작업 목록, 목표·완료 기준, 한글 다음 행동, 승인·진행 단계와 검증 결과를 표시한다. 명령 복사 버튼은 명령을 실행하지 않는다. `#task=TASK-ID`로 작업 상세를 직접 열 수 있다. 5초마다 갱신하며 상세 증적은 접어 볼 수 있다. 로컬 same-origin GET만 허용하고 변경 API·파일 browsing·원격 노출은 제공하지 않는다.

웹 페이지 크기는 20개, 조회 API/CLI 기본 limit은 100개(허용 1~200)이다. 검색·상태·프로젝트 필터와 page 이동을 지원한다. 전체 상태 건수/total은 모든 작업, filtered_total/pages는 필터 결과 기준이다. 검색 `%`/`_`는 문자 그대로 취급한다. 목록에 없는 task는 ID로 explain을 조회한다. DB 없는 기존 환경도 backlog 목록·상세·의존 작업·완료 기준을 읽는다. 기존 상태(done/review_ready/in_progress 등)는 화면에서만 정규 상태로 표시하고 원본 파일을 변경하지 않는다. 승인·검증 기록이 없으면 미수집으로 안내한다. 새 portable workspace의 기본 웹 진입점은 `devroster dashboard`다.

## 7. 통합 백업

```sh
devroster backup create /safe/my-project.zip --json
devroster backup inspect /safe/my-project.zip --json
```

Canonical DB(`state.db`)나 Studio DB(`studio/studio.db`) 중 하나 이상이 있어야 한다. 둘 다 있으면 같은 시점으로 함께 담는다(format 2, manifest `studio_present`). 웹 화면에서 만드는 백업은 canonical DB가 필요하다. 실행/검증/command/attempt/lease가 활성화되어 있으면 거부한다. 기존 writers를 직렬화한 상태에서 SQLite snapshot과 managed 설정·task 산출물을 복사하고, 입력이 변경되면 거부한다. backup은 managed config/output 바깥의 새 `.zip`에 저장한다.

포함: canonical·Studio DB, workspace marker, config/starter/data JSON·문서, task별 canonical/attempt/checkpoint/verification 산출물. 제외: .env/.mcp.json/auth stores, source Git, worktrees, venv/dependencies, process registry. config의 secret key/등록된 secret은 마스킹한다. DB·immutable evidence에서 알려진 unredacted secret을 발견하면 digest를 조용히 바꾸지 않고 백업을 거부한다.

Manifest의 경로/size/SHA256을 검사하며 전체 expanded 512MiB/10000 files, 일반 파일 64MiB(DB는 전체 한도) 제한이 있다. SHA 검사는 무결성 검사이며 서명/암호화가 아니다. 신뢰할 수 있는 bundle을 보관·복원한다. 검사만으로 source Git나 provider 인증이 복원 가능한 것은 아니다.

## 8. 복원

```sh
# destination은 아직 없는 새 폴더여야 한다(기존 폴더에는 복원하지 않음).
# 외부 설정을 쓰는 프로젝트도 bundle 안의 경로는 `.agent-dev/...`로 같고, 복원본은 새 project-local workspace다.
# 원래 workspace_id·소스 경로는 가져오지 않고, 공통 MCP 연결은 비활성 프로젝트 전용 연결로 복원된다.
devroster backup restore /safe/my-project.zip /path/to/restored-project --json
cd /path/to/restored-project
devroster doctor
# 소스 Git은 백업에 없으므로 별도로 준비하고 프로젝트 설정에서 모듈 경로를 다시 지정한다.
git clone SOURCE_URL /path/to/source-checkout
```

복원 전에 모든 entry/hash, DB integrity/FK/schema를 검사한다. traversal/중복/symlink/숨김 credential 경로를 거부한다. 임시 폴더에 풀어 검증한 뒤 덮어쓰지 않는 원자적 이동으로 새 폴더를 만들며, 이미 있는 폴더(빈 폴더 포함)에는 복원하지 않는다. 이 원자적 이동을 지원하지 않는 시스템에서는 복원을 거부한다.

복원본에서는 실행·게시 off, 승인 invalidated, lease fencing 증가, grants consumed, command cache 무효화, pending outbox dead, 비종료 task blocked, runner 인증 선언 false·tool disabled로 시작한다. 원본 DB/승인은 변경하지 않는다. immutable snapshot와 terminal evidence는 역사 자료로 보존한다.

목적지 repository/profile을 검토하고 source checkout과 provider 로그인을 준비한 후 새 design/plan/task 승인을 진행한다. 기존 task ID/승인으로 원래 workspace를 실행하려 해도 경로 검사가 거부한다. 복원된 task를 즉시 resume하지 않는다.

## 9. 검증 범위

- 새 테스트: project isolation/discovery, readonly doctor/query, auth 출력 보호, profile revision/relative worktree, backup/restore/revocation/tamper/secret/collision, HTTP 보안.
- 전체 회귀와 scoped lint/type/schema/compile/wheel 검증.
- `scripts/smoke_wheel.py`: clean venv에 wheel을 `--no-deps` 설치해 starter/new CLI가 legacy OpenAI 의존성 없이 동작하는지 확인. 두 프로젝트와 하위 cwd, fake phase runner/실제 local verification, bundle restore 검증. 기본 설치는 runtime dependencies가 없고 OpenAI는 선택 extra이다.
- 실제 브라우저로 desktop/mobile/API/data 렌더링, text-only evidence와 console 검사. 임시 fixture의 로컬 Codex login status probe 확인.
- 실제 AI 실행은 `scripts/smoke_live.py --run`으로 따로 확인한다(아래). GitHub/Figma/scanner 연동, Windows/Linux 실 OS와 remote GitHub CI 실행은 미검증. CI에는 Ubuntu/macOS/Windows × Python3.11/3.14 portable 검사와 installed-wheel smoke를 구성했다.

## 10. 기존 프로젝트 운영

이전 버전(JSON 파이프라인)의 작업 공간 자료는 `devroster refactor migrate`·`compare`로 가져와 비교한다. 절차는 [실행·이관·복원 가이드](operations.md)를 따른다.


## 11. 프로젝트 프리셋

```sh
devroster preset list --json
devroster preset show python --json
devroster preset write react /path/to/new-profile.json
devroster init --root /path/to/project --name MyProject --preset python
```

| preset | 필수 검증 | 작성 범위 기본값 |
|---|---|---|
| generic | git diff --check (공백만) | src/tests |
| python | pytest, Ruff, python -m build | src/tests/pyproject.toml |
| react | npm run lint, npm run test -- --run, npm run build | src/tests/public/package manifests |
| spring-boot | Maven wrapper -B verify | src/pom.xml |

프리셋은 프로젝트 의존성을 설치하거나 파일을 수정하지 않는다. Python dev tools, React npm scripts, Maven wrapper는 사용자가 source 프로젝트에 준비한다. React 테스트 명령은 `--run`을 지원하는 script 기준이므로 Jest 등 다른 도구는 profile을 수정한다. Gradle Spring 프로젝트도 검증 명령을 변경한다. `--preset auto`는 manifest 기반 추정이며 여러 manifest/없는 manifest는 explicit 선택을 요구한다. `preset write`는 기존 파일을 덮어쓰지 않는다. 기존 프로젝트에는 검토한 파일을 `devroster project profile PROJECT FILE`로 적용한다.

## 12. 안전한 workspace 업그레이드

```sh
devroster upgrade --json                        # 읽기 전용 사전 검사
devroster upgrade --apply --backup /safe/pre-upgrade.zip --json
devroster upgrade --recover --json              # 중단된 journal이 있을 때만
```

0.2.0→0.3.0 지원. package 자체는 pip로 먼저 설치한다. workspace marker에 engine_version을 기록하고 기존 최신 profile에 없는 execution limit 기본값을 추가한다. engine 버전 갱신과 DB schema는 별개다. SQLite schema는 서버·CLI가 DB를 열 때 추가형(additive) migration으로 현재 버전 3까지 올리며, v3 이전 DB는 올리기 전에 같은 폴더에 `state.db.pre-v3-backup`을 남긴다. 더 새 schema의 DB는 이전 버전 엔진이 열지 않는다. upgrade는 새 profile revision/event를 하나의 SQLite transaction으로 기록한다. 원래 approvals/snapshots/history를 수정하지 않는다. 이전 snapshot에 limit이 없으면 bounded compatibility defaults가 적용된다. 이미0.3.0이면 no-op이며 사용자 profile 변경은 별도 `project profile`로 적용한다.

실행/검증/command/lease가 활성 상태이면 거부한다. 적용 전 기존 bundle 규칙의 backup을 만들고, backup 중 DB/config가 바뀌면 거부한다. `--backup` 생략 시 프로젝트 상위의 `<project-name>-agent-dev-backups/` 아래 새 ZIP을 만든다. config는 private durable journal과 atomic replace로 갱신한다. 정상 실패는 DB/config를 rollback하고, 중단 시 `devroster upgrade --recover`가 DB commit event를 보고 finalize/rollback한다. journal 존재 시 작업 실행 및 추가 backup을 차단한다. 원본 config가 나중에 수정됐다면 자동 복구로 덮어쓰지 않고 수동 검토를 요구한다. 검증된 ZIP은 복구 수단이며 bundle restore는 기존 승인 해제 정책을 따른다. 지원하지 않는/newer workspace·schema는 거부한다.

## 13. 실행 한도와 사용량

```json
{"execution_limits":{"max_attempts":20,"max_runner_seconds":7200,"phase_timeout_seconds":1800,"max_reentries":1}}
```

프로젝트 profile을 적용한 뒤 새 plan 승인을 받으면 limit이 snapshot에 고정된다. 최대 attempts는 전체 task의 실패·quota·재진입·중단 시도를 포함한다. 누적 runner 시간은 여러 run/resume 요청에 걸쳐 합산한다. 중단 시 측정이 없으면 attempt 시작부터 종료/현재까지 시간을 보수적으로 사용한다. 재개 시 이전 lease의 running attempt는 새 fencing token으로 interrupted 처리한 뒤 limit을 검사한다. 한도를 이미 다 쓴 경우에도 증적을 정리하고 idle backup이 가능해진다. phase timeout은 남은 전체 시간과 지정 timeout 중 작은 값이다. 제한 도달 시 blocked/EXECUTION_LIMIT_REACHED가 되며 resume으로 초기화되지 않는다. 추가 작업이 필요하면 증적을 검토해 새 task를 승인한다.

범위: max_attempts1~1000, 총 시간1~86400초, phase1~7200초, reentry0~20. 사용량은 provider numeric input/output/cached input 값만 저장한다. 미수집 값은 null/미수집이며0이나 비용으로 추정하지 않는다. usage.measurements에 metric/value/known_attempts/unknown_attempts/complete를 제공한다. CLI 설정 오류나 control parser 실패는 ExecutionBlocked event의 안전한 reason으로 확인할 수 있다.

```sh
devroster task usage TASK-ID --json
devroster task list --search login --state blocked --project MyProject --limit 50 --page 2 --json
```

## 14. 실제 provider 샘플 검증

```sh
python scripts/smoke_live.py --run --provider codex_cli --report /safe/live-report.json
# 로컬 CLI가 지원하는 모델을 이 샘플에만 지정하려면 --model MODEL
```

소스 제공 `scripts/smoke_live.py`는 명시 `--run`에만 실제 provider를 사용한다. 임시 Git 프로젝트에서 기본 Workflow(분석 승인 단계는 fixture로 승인)를 AI 호출 최대 6회, 총 900초·단계 300초 안에서 실행하고 stdlib unittest gate로 검증한다. 원래 source checkout 보존과 게시 미수행을 확인한다. 전역 모델/인증을 바꾸지 않고 provider 원문·계정 정보를 report에 남기지 않는다. CI의 wheel smoke는 fake runner이며 live 검증과 구분한다.


## 15. 파일 편집 없는 프로젝트 설정

`devroster setup --web --root PROJECT` 또는 대화형 `devroster setup --root PROJECT`로 루트·프론트·백·AI 실행파일·모델·검증·예산·환경을 설정한다. `devroster init`과 기존 프로필 명령도 유지한다. 내보낸 파일은 `devroster setup --from FILE --copy --root NEW-PROJECT`로 인증 값 없이 재사용한다. 새 작업 승인에 프로필을 반영하며 기존 snapshot은 유지한다. 자세한 단계·진단·복구 범위는 [설정 사용 가이드](setup-user-guide.md)를 따른다.
