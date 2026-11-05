# 프로젝트·AI·환경 설정 사용 가이드

## 1. 시작

Python 3.11+와 이 패키지를 설치한 뒤 실행한다. 별도 웹 빌드나 DB 서버는 필요하지 않다.

```sh
python -m pip install /path/to/devroster
devroster setup --web --root /path/to/my-project
```

`http://127.0.0.1:8768/setup`을 연다. Studio·개발 현황에는 설정 링크가 있다. 터미널에서 질문에 답하려면 `devroster setup --root /path/to/my-project`를 사용한다. 프로젝트 안에서는 `devroster setup`만 실행해도 된다. 하위 폴더에서 실행하면 초기화한 상위 프로젝트를 찾는다.

## 2. 웹에서 준비하기

| 순서 | 입력·행동 | 의미 |
|---|---|---|
| 1 프로젝트 | 최상위 폴더·폴더 선택·설정 불러오기 | 다른 폴더의 설정을 덮어쓰려면 그 폴더의 최신 설정을 먼저 불러온다 |
| 모듈 | source/frontend/backend·경로·기술·허용 경로 | 상대 경로는 최상위 기준, 별도 저장소는 절대 경로 가능 |
| 2 AI | 기본 도구·사용 여부·실행파일·모델 | 빈 모델은 해당 CLI의 기본값, 기본 도구를 우선하며 다른 활성 도구는 기존 fallback 정책에 사용 |
| 3 개발 역할 | 도메인 프로필·역할 순서(↑↓)·선택 역할 추가/삭제·새 역할(이름·권한·지시문) | 기본 14역할은 삭제 불가. 역할을 바꿔 저장하면 Workflow를 이 순서로 다시 만든다 ([reuse.md](reuse.md) 프로젝트 역할 구성) |
| 4 검증 | preset 명령 가져오기 또는 직접 추가 | 프로젝트의 실제 테스트·빌드 명령으로 수정. 필수 검증 하나 이상 |
| 고급 | 예산·시간·포트 | 다음 승인 작업·서버 실행부터 적용 |
| 5 환경 | 이름·숨겨진 값·행 삭제 | 마스크 유지=값 유지, 삭제 후 저장=변수 삭제 |
| 6 저장·진단 | 로그인 확인 선택·저장·환경 진단 | 저장된 설정을 검사. 조치 안내와 자세한 기술 정보를 분리해서 표시 |

새 폴더라면 **없는 최상위·프로젝트 내부 모듈 폴더를 저장할 때 만들기**를 선택한다. 프로젝트 바깥의 별도 저장소는 먼저 준비해야 한다. 저장은 Git 초기화·commit·개발 실행·배포를 자동 수행하지 않는다.

예: 최상위 `/work/my-app`, frontend `frontend` (React), backend `backend` (Spring Boot). 공통 소스 모듈이 필요 없다면 source 행을 삭제하고 프론트·백 검증 명령을 추가한다. 같은 Git 저장소이면 한 작업 저장소로 묶고 `frontend/src`, `backend/src` 같은 허용 경로와 모듈별 검증 작업 폴더를 만든다. 별도 Git 저장소이면 각각 작업 저장소를 만든다.

Git 초기화나 저장소 구성이 바뀌면 설정을 다시 저장해야 한다. 등록 경로가 Git 하위 폴더이면 진단과 작업 승인에서 재저장을 안내한다.

## 3. AI 도구와 실제 적용

1. CLI를 사용할 때 Codex·Claude·Gemini CLI를 설치한다. API 사용은 CLI 설치 없이 아래 연결 절차를 따른다.
2. 터미널에서 `codex login`, `claude auth login`, 또는 `gemini` 로그인을 진행한다.
3. 설정 화면에서 실행파일·모델을 지정하고 **로그인 상태도 확인**을 선택해 저장한다.
4. **환경 진단**의 설치·로그인·모델 안내를 확인한다.
5. Studio를 새로 열거나 새로고침한다. 저장한 기본값이 채워진다.

모델 이름은 계정에서 지원하는 값을 사용한다. 로컬 모델 목록은 참고 정보이며 실제 계정 권한을 확인하는 호출은 아니다. API 인증을 쓰는 CLI라면 `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` 등의 환경변수를 설정할 수 있다. 계정 로그인 상태 검사와 API 키 사용 가능 여부는 다를 수 있다.

| 실행 경로 | 설정 반영 |
|---|---|
| Studio | 기본 provider/model/command, 이번 생성의 UI 선택으로 모델·도구 override. 도구·MCP·파일 접근 차단 유지 |
| 승인된 개발 작업 | 새 프로필로 승인하는 작업에 반영. 기존 snapshot·진행 중 작업은 유지 |
| 기존 legacy CLI | 기존 엔진 루트에서 설정을 저장하면 runner/model/command·소스 경로·환경 이름을 runtime에 반영 |

저장된 프로젝트 프로필이 아직 없는 legacy 엔진은 현재 runtime의 CLI 모델·실행파일·fallback 순서·저장소 경로·표시 이름을 먼저 불러와 검토할 수 있다.

legacy의 기존 DB·알림·MCP 상세 설정은 기존 웹 설정과 `devroster config`를 계속 사용한다. Stitch·Figma 화면은 legacy·신규 모두 `/references`에서 연결·적용한다. 새 프로젝트의 portable 승인 흐름에는 `devroster tool register` / `devroster tool grant`로 사용할 도구·권한을 등록한다. Studio의 기획·목업 생성에는 해당 도구가 연결되지 않는다.

API 도구는 **AI 도구** 카드에서 주소·모델·인증 환경변수 이름·출력 토큰 상한을 지정한다. **인증 키 입력란 추가** 후 아래 환경변수의 숨겨진 값에 키를 입력하고 저장한다. API는 `text.generate` 전용이며 개발 단계에는 CLI가 필요하다. API 진단은 키 존재와 설정을 확인하고 실제 인증·모델 접근을 보장하지 않는다. 상세 연결 예시는 [다중 AI 사용 가이드](multi-ai-user-guide.md)를 따른다.

### 작업별 모델·effort 자동 선택

1. 도구 카드에서 **작업에 따라 자동 선택 (균형형)**을 선택한다.
2. 단순·일반·복잡 작업 각각의 **사용할 모델**과 **모델 지원 강도**를 입력한다. 같은 모델을 세 등급에 등록해 강도만 바꿔도 된다.
3. Codex의 **로컬 CLI 모델 후보로 채우기**는 이 컴퓨터의 공개 모델 캐시에서 후보를 채운다. 목록이 없으면 직접 입력한다. 후보의 이름으로 분류한 편의 기능이며 가격·계정 사용 가능 여부를 보장하지 않는다. Claude는 직접 등록한다.
4. 자동 선택의 최대 강도를 정하고 **작업별 선택 미리보기**에서 작업 제목·단계를 확인한다.
5. 저장 후 다음 승인 작업과 Studio 생성부터 적용한다.

터미널 `devroster setup`에서도 도구별 `fixed/auto`, 고정 강도, 작업 등급의 모델·지원 강도·상한을 질문한다. JSON 내보내기/가져오기/다른 프로젝트 복사에도 정책이 포함된다.

| 기준 | 기본 목표 |
|---|---|
| 문서·표현·이름 변경 중심 | low |
| 일반 구현·테스트·미분류 작업 | medium |
| 설계·명세 종합·보안/권한/데이터 위험·여러 영역/큰 범위 | high |

위험 신호와 단계 역할을 우선하며, 작업 제목·범위·작업 영역으로 판단한다. 정해진 규칙이므로 같은 입력·정책이면 같은 결과를 얻는다. 예를 들어 README도 함께 수정하는 일반 기능 구현은 문서 작업으로 낮추지 않는다. 자동 선택은 등록한 해당 도구의 모델 안에서만 수행하고, 도구 간 fallback은 기존 실행 정책을 따른다.

지원 강도는 쉼표로 입력한다(예: `low, medium, high`). 목표·상한 이하에서 지원하는 가장 높은 강도를 선택한다. 선택 가능한 강도가 없으면 저장을 거부한다. 목록을 빈칸으로 남기면 CLI 기본 강도를 사용하며 미리보기·이유에 표시한다. **이 경우 CLI 기본 강도에 설정 상한을 강제할 수 없으므로, 상한을 적용하려면 지원 강도를 등록해야 한다.** 계정/조직의 강도 제한은 CLI에서 추가로 적용될 수 있다.

`xhigh`·`max`는 지원하는 도구·모델의 고정 모드에서만 지정할 수 있다. Claude API는 `xhigh`, Gemini API·Grok API는 `xhigh`·`max`를 거부한다. Gemini CLI는 강도와 지원 강도 목록을 비우고 모델만 자동 선택한다. 호환 API에서 강도를 전달하려면 지원 여부를 확인하고 추론 옵션 `native`를 선택한다. 균형형 자동 선택은 최대 `high`이다. `ultra`·`ultracode`는 별도 자동 위임 동작을 포함하므로 이 설정에서 지원하지 않는다. 시간·시도 예산은 기존대로 적용되며, 실패만으로 더 높은 모델/강도에 자동 재시도하는 정책은 추가하지 않는다.

```sh
devroster setup --preview-task "README 문구 수정" --root /path/to/my-project
devroster setup --preview-task "로그인 권한 개선" --preview-phase security --root /path/to/my-project
```

미리보기는 저장·AI 호출을 수행하지 않는다. 실제 실행은 더 자세한 작업 범위를 포함하므로 제목만 입력한 미리보기와 분류가 달라질 수 있다.

Studio는 생성 시작 시 선택을 확정하고 **최근 생성·변경 이력**에 모델·강도·이유를 기록한다. **프로젝트의 선택 정책 적용**을 해제해 이번 생성의 모델·강도를 고정할 수도 있다. 생성 도중 설정 변경은 해당 생성에 적용하지 않는다. 승인 workflow는 snapshot의 등록 정책을 사용하며 `RunnerUsage.selection` 이벤트에 선택 결과를 남긴다. legacy 실행은 `pipeline-state.json`의 단계별 `model_selections`에 저장한다.

공식 옵션: [Codex model_reasoning_effort](https://learn.chatgpt.com/docs/config-file/config-reference), [Claude --effort](https://code.claude.com/docs/en/model-config). 동일한 강도 이름이 모델 간 동일한 계산량이나 가격을 뜻하지 않는다.

## 4. 환경변수·추가 준비 항목

| 항목 | 설정 예·확인 |
|---|---|
| 앱 서버 | `VITE_API_BASE_URL`, 서비스가 요구하는 포트·URL. 앱 실행 환경에도 연결 |
| DB | `DATABASE_URL`, `DB_PASSWORD`. 실제 DB 실행·접속·migration은 별도 프로젝트 절차 |
| Redis·스토리지 | `REDIS_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |
| AI API | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` |
| 디자인·GitHub | `STITCH_API_KEY`, `FIGMA_ACCESS_TOKEN`, `GITHUB_PAT_TOKEN`; MCP 연결·권한 별도 |
| 알림 | `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`; 기존 알림/봇 설정에도 연결 |
| 개발 도구 | React: Node/npm, Python: Python/pytest/Ruff/build, Spring: Java/Maven wrapper, 필요 시 Docker |
| 검증 | npm scripts·Gradle/Maven·pytest 등 실제 프로젝트 도구에 맞는 argv/cwd |
| Git | 소스 저장소·초기 commit·기준 ref·관리 폴더와 output ignore |

승인 snapshot에는 환경 이름만 들어간다. 값은 실행 시 로컬 파일에서 읽어 해당 프로세스에만 전달한다. 값의 회전은 기존 승인 이름에 적용되며, 승인된 이름의 값을 삭제하면 해당 작업 실행이 중단되어 복원을 안내한다. Studio에는 AI 인증 키만 추가로 전달한다. HOME/PATH/PYTHONPATH/NODE_OPTIONS 등 실행 환경 주입 키는 이 폼에서 지원하지 않는다. 도구 경로는 실행파일 입력란에서 설정한다.

## 5. 조회·진단·재사용

```sh
devroster setup --show --root /path/to/my-project
devroster setup --check --check-auth --root /path/to/my-project
# 내보낸 설정을 같은 프로젝트에서 다시 적용
devroster setup --from project-settings.json --root /path/to/my-project
# 새 프로젝트에 복사: 프로젝트 ID/버전/내부 경로를 대상에 맞춤, 비밀 값 제외
devroster setup --from project-settings.json --copy --root /path/to/another-project
devroster setup --web --root /path/to/another-project
```

새로 복사한 환경변수 값은 빈칸이므로 대상 프로젝트에서 다시 입력한다. 대상에 이미 저장한 값과 추가 변수는 유지한다. 외부 저장소 경로는 유지되므로 모듈 경로를 검토한다. `--copy`는 대상의 최신 버전을 기준으로 설정을 적용한다. 일반 `--from`은 파일의 revision/profile_version이 맞지 않으면 충돌을 반환한다. `--check-auth`는 설정 파일 적용 시에도 실제 로그인 상태를 확인해 등록한다.

진단은 실제 테스트·DB 접속·Docker 실행·개발 작업을 대신하지 않는다. 웹 포트가 사용 중이면 이미 실행한 서버인지 확인하거나 포트를 바꾼다. `devroster setup --web --port 8872`처럼 실행 옵션으로 포트를 바꿀 수도 있다.

## 6. 저장·백업·복구

| 파일 | 용도 | 백업 범위 |
|---|---|---|
| `.agent-dev/state.db` | 설정 revision·프로필 이력·기존 승인 실행 상태 | 기존 `devroster backup`에 포함 |
| `.agent-dev/environment.json` | private 환경 값 | `devroster backup`·설정 export에서 제외; 필요 시 별도 private 백업 |
| `.agent-dev/config/profile.json` | DB 프로필의 호환 캐시 | 기존 `devroster backup`에 포함 |
| `.agent-dev/config/runtime.json` | 실행 gate·서버와 legacy 설정 | 기존 `devroster backup`에 포함 |
| `.agent-dev/studio/studio.db` | 기획·명세·목업·코멘트 | `devroster backup`(format 2)에 포함 |

legacy 엔진의 config 캐시는 기존 엔진 `config/`에 저장한다. 실제 값은 최상위 프로젝트 `.agent-dev/environment.json`에 저장하며 DB·프로필·감사 이벤트·공개 API에 넣지 않는다. 관리 폴더는 0700, private 파일은 0600으로 저장한다. Windows의 접근 제어는 해당 계정·폴더 권한에도 따른다.

환경 파일은 원자적으로 교체하며 설정 트랜잭션 실패 시 이전 값으로 되돌린다. 프로세스가 파일 교체 직후 종료되면 값 회전이 DB 갱신보다 먼저 적용될 수 있다. 승인된 환경 이름과 snapshot은 DB 기준으로 유지된다. 저장 경고가 나오면 권한을 확인하고 설정을 다시 저장해 캐시·폴더를 복구한다.

복원 후에는 환경 값·CLI 로그인을 다시 준비하고 진단한다. 실행·publish gate는 기존 승인 절차로 활성화한다.

## 7. 공통 설정과 저장 위치

설정 화면 소개 문단의 **공통 설정** 링크(`/settings`)에서 여러 프로젝트가 함께 쓰는 값을 관리한다.

| 구역 | 하는 일 | 주의 |
|---|---|---|
| 공통 기본값 | 기본 AI 도구·모델·추론 강도, 실행 한도 기본값과 상한 | 소스 범위·검증 명령·실행/publish 권한·환경변수는 공통값으로 상속하지 않는다. 다른 화면에서 먼저 저장했으면 충돌로 표시한다 |
| 이 프로젝트에 반영 | 항목마다 "공통값 사용" 또는 "직접 지정"을 고르고 반영 | 항목을 지우면 상속으로 돌아간다. 빈 모델은 "CLI 기본 모델"이라는 값이다. 반영하면 새 프로필이 생기고 기존 승인·snapshot은 그대로다. 공통값이 바뀌면 "공통 기본값이 바뀌었습니다"가 표시된다 |
| 공통 MCP 연결 | 연결 정의 등록·비활성화, 이 프로젝트에서 버전·자료 범위·도구 선택 | 공통 연결이 허용한 범위 안에서만 고를 수 있다. 비활성화하면 모든 프로젝트에서 즉시 막힌다. 인증 값은 화면·DB·내보내기에 나오지 않는다 |
| 저장 위치 | 관리 폴더·설정 위치 확인, 사전 점검 → 실행 → 복구 | 점검 결과와 원본 보존 안내를 본 뒤 실행한다. 실행 후 웹·CLI·앱을 다시 연다 |

공통 설정은 `<관리 폴더>/global/settings.db`에 revision으로 저장한다. 공통값을 저장해도 어떤 프로젝트도 바뀌지 않는다. CLI는 저장 위치 명령(`devroster settings location ...`)을 제공한다. 자세한 이전 절차는 [재사용 가이드](reuse.md)의 "설정 위치를 소스와 분리하기"를 참고한다.

## 8. 역할별 스킬

설정 화면 3단계 "개발 역할"의 **역할별 스킬**에서 저장소마다 기술 스택에 맞는 스킬 묶음을 고른다. 스킬은 DevRoster가 검토해 버전(commit)과 파일 digest를 고정한 공개 스킬만 사용한다.

| 동작 | 설명 |
|---|---|
| 스택 감지로 제안 | 저장소의 `package.json`, `build.gradle`, `pom.xml`, `pubspec.yaml`, `Package.swift`, `Dockerfile`, `*.tf` 등을 읽어 묶음을 체크만 한다. 저장해야 적용된다 |
| 설정 저장 | 선택이 새 프로필 revision으로 저장된다. 이미 승인된 작업과 재시도는 이전 선택을 그대로 쓴다 |
| 저장소에 설치 | 고정 commit을 받아 digest를 확인한 뒤 `<저장소>/.agents/skills/<이름>`에 복사하고 `.claude/skills/<이름>`을 연결한다. git에는 `.git/info/exclude`로 잡히지 않는다. 같은 이름의 직접 만든 스킬은 덮어쓰지 않는다 |

- 묶음: core(모든 프로젝트), security-scan, web-react, spring, postgres, container(Docker·Kubernetes), terraform, mobile-expo, mobile-flutter, mobile-ios, mobile-android. 목록은 `devroster skills catalog`로 본다.
- 개발 작업에는 각 역할에 맞는 스킬만 프롬프트에 들어간다(노드당 최대 10개). 스킬 본문은 관리 폴더의 읽기 전용 캐시(`<관리 폴더>/skills/`)에서 열람한다.
- 설치 후 Claude Code·Codex를 다시 열면 스킬이 보인다. Codex는 실행한 git 저장소 안의 `.agents/skills`만 찾는다.

```sh
devroster skills catalog --root PROJECT   # 묶음과 스킬 목록
devroster skills suggest --root PROJECT   # 저장소별 감지 결과와 제안
devroster skills status --root PROJECT    # 저장소별 설치 상태
devroster skills sync --root PROJECT      # 저장한 선택대로 설치·정리 (--dry-run: 계획만)
```

## 9. AI 도구 (공통)

메뉴 **설정·자료 → AI 도구**(`/ai`)에서 모든 프로젝트가 함께 쓰는 AI 도구를 관리한다.

| 구역 | 하는 일 |
|---|---|
| 도구별 설정 | Codex·Claude·Gemini CLI와 API 도구의 사용 여부, 실행파일(이름 또는 절대 경로), 모델, 추론 강도. CLI는 DevRoster 전용 로그인과 "로그인 상태 확인" |
| 기본 순서 | 새 프로젝트가 도구를 고를 때의 순서 |
| API 키 | OpenAI·Claude·Gemini·Grok(xAI) API 키. 값은 `<관리 폴더>/credentials/shared/ai-providers.json`(0600)에만 저장하고 화면·DB·내보내기에 나오지 않는다. 프로젝트 환경변수에 같은 이름이 있으면 그 프로젝트는 자기 키를 쓴다 |
| 사용 중인 프로젝트 | 공통 AI 도구를 쓰는 등록 프로젝트 |

- 저장하면 새 revision이 생기고 **다음에 승인하는 작업부터** 적용된다. 이미 승인된 작업과 재시도는 승인 시점의 도구·모델을 그대로 쓴다. 레거시 파이프라인은 다음 실행부터 적용된다.
- 프로젝트 설정 2단계 **사용할 AI**에서는 공통 도구 중 쓸 도구를 체크하고 순서만 정한다. 위의 도구가 먼저 쓰이고, 첫 번째 도구가 기획·목업 생성의 기본 도구다. 아무것도 고르지 않으면 준비 모드가 되어 AI 실행을 할 수 없다.
- 기존 프로젝트는 2단계의 **공통 AI 도구로 전환**을 누른다. 공통 AI 도구가 없으면 이 프로젝트의 현재 설정으로 처음 만들고, 이미 있으면 다른 값을 먼저 보여준 뒤 확인을 받는다. 실행에 쓰는 도구·모델·강도는 전환 전과 같다.
- `/settings` 공통 설정의 "기본 AI 도구" 값은 공통 AI 도구를 쓰는 프로젝트에는 쓰이지 않는다.

```sh
devroster ai show                       # 공통 AI 도구, 로그인 상태, 키 저장 여부(값 제외)
devroster ai set FILE [--expected-revision N]
devroster ai import --root PROJECT      # 전환 미리보기, --apply 로 적용
```

