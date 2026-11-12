# DevRoster

**기획서 한 장에서 검증된 코드까지 — 내 컴퓨터에서, 이미 쓰고 있는 AI 도구로.**

> **English summary** — DevRoster is a local development workbench. You write a plan document; it drafts specifications and clickable mockups, and after you approve them it runs a roster of AI roles (planner, architect, developer, QA, reviewer, …) through the AI CLIs you already use — Codex, Claude Code, Gemini — in isolated Git worktrees. Every task runs from an approved snapshot, must pass your own test and build commands, and finishes with a local fast-forward merge or a GitHub pull request. Everything stays on your machine. The UI and docs are in Korean.

| 한눈에 | 내용 |
|---|---|
| 무엇 | 기획서 → 명세서·목업 → 승인 → AI 역할 개발 → 내 검증 → 병합/PR |
| 어디서 | 내 컴퓨터에서만 실행 (서버·계정 불필요) |
| 무엇으로 | 이미 쓰는 AI CLI(Codex·Claude Code·Gemini) 또는 API 키 |
| 저장 위치 | 프로젝트의 `.agent-dev/`, 작업 산출물은 `output/` |

**목차**

- [무엇을 하나요](#무엇을-하나요)
- [주요 기능](#주요-기능)
- [요구 사항](#요구-사항)
- [설치](#설치)
- [빠른 시작](#빠른-시작)
- [화면](#화면)
- [명령어](#명령어)
- [지원 AI 도구](#지원-ai-도구)
- [데이터와 보안](#데이터와-보안)
- [문서](#문서)
- [개발에 참여하기](#개발에-참여하기)
- [설치 파일 만들기](#설치-파일-만들기)
- [라이선스](#라이선스)

## 무엇을 하나요

```text
기획서 ─▶ 명세서·목업 ─▶ 화면 검토·확정 ─▶ 설계·계획 승인 ─▶ 역할별 AI 개발 ─▶ 검증 ─▶ 완료
 (작성)     (AI 생성)        (사람)            (사람)         (AI CLI)       (내 테스트)  (로컬 병합/PR)
```

| 단계 | 누가 | 결과 |
|---|---|---|
| 기획서 작성 | 사람 | 요구사항이 담긴 기획서 |
| 명세서·목업 생성 | AI | 요구사항·수용 기준이 연결된 명세서, 검토용 목업 |
| 화면 검토·확정 | 사람 | 요소별 의견이 반영된 확정 화면 |
| 설계·계획 승인 | 사람 | 설정·역할·규칙이 고정된 snapshot |
| 역할별 개발 | AI CLI | 역할 순서대로 만든 변경 |
| 검증 | 내 테스트·빌드 명령 | 통과 또는 실패 로그와 함께 개발 역할로 되돌아감 |
| 완료 | 사람 + DevRoster | 기본 브랜치 빠른 병합(fast-forward) 또는 GitHub PR |

**다섯 가지 원칙**

- **기획부터 시작합니다.** 기획서를 쓰면 AI가 요구사항·수용 기준이 연결된 명세서와 검토용 목업을 만듭니다. 화면 요소마다 의견을 남기고 확정합니다.
- **사람이 승인한 범위만 실행합니다.** 설계와 계획을 승인하면 그 시점의 설정·역할·규칙이 고정(snapshot)되고, AI는 그 범위 안에서만 작업합니다.
- **역할을 나눠 일합니다.** Planner, Architect, DBA, UX/UI, Developer, QA, Security, Reviewer, Tech Writer 같은 역할이 순서대로 실행됩니다. 연속된 역할은 한 번의 AI 호출로 묶어 비용을 줄입니다.
- **내 검증 명령으로 확인합니다.** 테스트·빌드 명령이 실패하면 실패 로그와 함께 개발 역할로 되돌아가 고칩니다.
- **원본 저장소를 건드리지 않습니다.** 작업마다 전용 Git worktree에서 진행하고, 완료할 때 기본 브랜치에 빠른 병합(fast-forward)하거나 GitHub PR로 냅니다.

## 주요 기능

| 영역 | 기능 |
|---|---|
| **기획** | 기획서 템플릿 · 명세서 22종 자동 생성 · 목업 생성 · 요소별 코멘트·화면 확정 · 개발 인계 |
| **실행** | 승인 snapshot · 역할 기반 Workflow(편집 가능한 DAG) · 세션 묶음 실행 · 작업 전용 worktree · 다중 저장소(프론트·백엔드) 분할 |
| **검증** | 모듈별 검증 명령 · 실패 시 자동 재진입 · 변경 검토 화면(코드·화면 캡처) · 요구사항↔테스트 추적 · 요구사항 변경 영향 분석 |
| **AI 도구** | Codex·Claude·Gemini CLI(앱 전용 로그인) · OpenAI·Claude·Gemini·Grok·K-EXAONE API · OpenAI 호환·로컬 모델(Ollama) · 작업 난이도별 모델·추론 강도 자동 선택 |
| **지식·연동** | 승인 기반 프로젝트 위키 · 역할별 스킬 묶음 · MCP 도구 연결 카탈로그 · Stitch·Figma 디자인 참고 · 슬랙 요청·승인·상태 알림 |
| **프로젝트 AI 규칙** | `.ai/`의 규칙·스킬·훅을 모든 AI 도구(CLI·API)와 Studio에 같게 적용 · CLAUDE.md·AGENTS.md·GEMINI.md 자동 동기화 · 승인한 훅만 실행 |
| **운영** | 사용량·예상 비용 집계 · 백업·복원 · 저장 위치 이전 · 데스크톱 앱 빌드 |

## 요구 사항

| 항목 | 필요한 것 |
|---|---|
| **실행 환경** | Python 3.11 이상, Git |
| **AI 도구** | 아래 [지원 AI 도구](#지원-ai-도구) 중 하나 이상 |
| **운영체제** | macOS, Linux, Windows |
| **개발 대상** | Git 저장소이고 첫 커밋이 있어야 하는 프로젝트 |

- **AI 도구 하나 이상**: [Codex CLI](https://github.com/openai/codex), [Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Gemini CLI](https://github.com/google-gemini/gemini-cli) 중 하나(파일 수정이 가능한 개발 역할에 필요), 또는 API 키(명세·목업 생성과 텍스트 단계에 사용)
- **Windows**: 소스 탐색·검색이 폴더 단계마다 링크·정션을 막고 연 파일을 다시 대조하는 방식으로 동작합니다.

> **팁**: 명세서·목업만 만들고 싶다면 API 키만 있어도 됩니다. 파일을 고치는 개발 역할까지 쓰려면 CLI 도구가 하나 이상 필요합니다.

## 설치

세 가지 방법 중 하나를 고릅니다.

| 방법 | 이런 경우 | 명령·파일 |
|---|---|---|
| **앱으로 설치(권장)** | 데스크톱 앱으로 쓰고 싶다 | 저장소를 받아 `scripts/build_macos.command` · `scripts\build_windows.bat` 실행 |
| **명령어로 설치** | 클론 없이 빠르게 설치 | `uv tool install` 또는 `pipx install` |
| **소스로 설치** | 개발에 참여한다 | `git clone` 후 `pip install -e .` |

### 앱으로 설치 (권장)

저장소를 받아 내 컴퓨터에서 앱을 만듭니다. Python 3.11 이상이 필요하고, 처음 한 번은 몇 분 걸립니다.

```sh
git clone https://github.com/edcs960/devroster.git    # 또는 GitHub에서 Code → Download ZIP
```

| OS | 실행할 파일 | 결과 |
|---|---|---|
| **macOS** | Finder에서 `scripts/build_macos.command`를 더블클릭 | `DevRoster.app`을 만들고, 원하면 Applications 폴더에 설치합니다. 내 Mac에서 만든 앱이라 "확인되지 않은 개발자" 경고 없이 열립니다 |
| **Windows** | 탐색기에서 `scripts\build_windows.bat`를 더블클릭 | `dist-app\DevRoster.exe`를 만듭니다. 바탕 화면 등 원하는 곳에 옮겨 더블클릭합니다 |

- ZIP으로 받았거나 macOS에서 더블클릭이 막히면 터미널에서 `bash scripts/build_macos.command`로 실행합니다.
- 새 버전은 저장소를 갱신(`git pull`)한 뒤 같은 파일을 다시 실행하면 됩니다.
- 앱을 실행하면 브라우저에 DevRoster 화면이 열립니다. 이미 실행 중이면 그 화면을 다시 엽니다. 끌 때는 화면 왼쪽 메뉴 맨 아래의 **앱 종료**를 누릅니다.
- AI CLI(Claude Code·Codex 등)는 따로 설치하고 설정 화면에서 로그인합니다.

### 명령어로 설치

클론 없이 설치합니다([uv](https://docs.astral.sh/uv/) 또는 pipx).

```sh
uv tool install git+https://github.com/edcs960/devroster    # 또는: pipx install git+https://github.com/edcs960/devroster
devroster app
```

### 소스로 설치

개발에 참여할 때 사용합니다.

```sh
git clone https://github.com/edcs960/devroster.git
cd devroster
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/devroster --help
```

어디서나 `devroster`로 쓰려면 PATH에 추가합니다.

```sh
ln -s "$PWD/.venv/bin/devroster" ~/.local/bin/devroster   # 예시
```

> **참고**: 런타임 의존성은 없습니다(표준 라이브러리만 사용). AI CLI는 각 도구의 안내대로 따로 설치합니다.

## 빠른 시작

```sh
devroster app --root /path/to/my-project
```

브라우저가 열리면 순서대로 진행합니다.

| # | 화면 | 할 일 |
|---|---|---|
| 1 | **프로젝트 설정** | 폴더와 모듈(프론트·백엔드 등)을 확인합니다. React·Python·Spring Boot 프로젝트는 빌드 파일을 보고 검증 명령을 미리 채웁니다. 저장 후 **환경 진단**을 실행합니다. |
| 2 | **AI 도구** | 사용할 도구를 켜고 **앱 전용 로그인**을 합니다. DevRoster는 개인 터미널의 로그인·설정과 분리된 전용 폴더로 AI CLI를 실행합니다. |
| 3 | **Studio** | 기획서를 쓰거나 Markdown 파일을 가져와 명세서와 목업을 만들고, 화면을 검토해 확정합니다. |
| 4 | **개발 인계** | 확정한 내용으로 설계·계획을 만들고 각각 승인합니다. |
| 5 | **실행 → 검토 → 완료** | 작업을 실행하고, 변경 검토 화면에서 코드와 화면을 확인한 뒤 완료합니다. |

> **주의**: 작업을 등록했다고 개발이 바로 시작되지는 않습니다. 4단계의 설계·계획 승인이 있어야 실행됩니다.

자세한 순서는 [웹 사용 가이드](docs/web-user-guide.md)에 있습니다.

## 화면

`devroster app` 하나로 모든 화면을 씁니다. 왼쪽 **☰ 전체 메뉴**에서 이동합니다.

| 화면 | 하는 일 |
|---|---|
| 프로젝트 홈 `/home` | 다음 할 일과 준비 상태 |
| Studio `/studio` | 기획서 → 명세서 → 목업 → 화면 확정 → 개발 인계 |
| 개발 흐름 `/workflows` | 역할 순서·단계 편집, 프리셋, 버전 비교 |
| 작업실 `/agent` | 작업 상태·검증 결과 확인, 실행·재개·검증·게시·완료, 실행 기록 (이전 개발 현황 `/dashboard` 주소는 이 화면으로 이동) |
| 사용량·진행 현황 `/usage` | 작업 진행 상태와 완료율, 기간별 AI 호출·토큰·예상 비용(도구·모델·역할·작업·일별), Studio 기획 생성과 그 기획으로 만든 작업을 합친 기획별 사용량 |
| 변경 검토 `/review` | 파일별 변경, 화면 캡처, 코드·화면 검토 결정 |
| 요구사항 근거·변경 `/traceability` `/changes` | 요구사항과 테스트 연결, 변경 영향과 후속 작업 |
| 소스 탐색 `/source` | 코드 검색, 관계 그래프(Graphify 선택) |
| 설정 `/setup` `/ai` `/integrations` `/references` `/wiki` `/messengers` | 프로젝트, AI 도구, MCP 연결, 디자인 참고, 위키 승인, 슬랙 연동 |

**어디서 시작할까**

- 처음 설정 → `/setup` → `/ai`
- 기획부터 → `/studio`
- 작업 상태·실행 → `/agent`
- 결과 확인 → `/review`

## 명령어

웹에서 할 수 있는 일은 터미널에서도 할 수 있습니다. `devroster`만 입력하면 전체 목록이, `devroster <명령> --help`로 세부 옵션이 나옵니다.

**실행·설정**

| 명령 | 설명 |
|---|---|
| `devroster app` | 웹 화면 실행 (`--root`, `--port`) |
| `devroster setup` | 프로젝트 설정 (`--web`, `--check --check-auth`, `--show`) |
| `devroster doctor` | 설치·로그인·저장소 진단 |

**작업·승인**

| 명령 | 설명 |
|---|---|
| `devroster task list` · `run` · `verify` · `resume` · `complete` | 작업 조회·실행·검증·재개·완료 |
| `devroster design` · `plan` | 설계·계획 등록과 승인 |

**규칙·연동·운영**

| 명령 | 설명 |
|---|---|
| `devroster rules show` · `sync` · `check` · `hooks` | 프로젝트 AI 규칙·스킬·훅(`.ai/`) 확인, CLAUDE.md·AGENTS.md·GEMINI.md 동기화, 훅 승인 |
| `devroster messenger listen` | 슬랙 리스너 실행 |
| `devroster backup` | 작업 공간 백업·복원 |

## 지원 AI 도구

| 도구 | 준비할 것 | 쓰이는 곳 |
|---|---|---|
| **Codex / Claude / Gemini CLI** | 설치, 앱 전용 로그인 | 개발 역할(파일 읽기·수정), Studio 생성 |
| **OpenAI / Claude / Gemini / Grok API** | 모델 이름, API 키 | Studio 생성, 텍스트 단계 |
| **K-EXAONE API (LG)** | `EXAONE_API_KEY` (모델 `k-exaone_v2.5` 기본 입력) | Studio 생성, 텍스트 단계 |
| **OpenAI 호환 API · Ollama** | 주소, 모델 이름 | Studio 생성, 텍스트 단계 |

> **주의**: API는 파일을 직접 수정하지 않습니다. 실제 개발에는 CLI 도구가 하나 이상 필요합니다.

자세한 연결 방법은 [다중 AI 가이드](docs/multi-ai-user-guide.md)를 참고하세요.

## 데이터와 보안

| 항목 | 내용 |
|---|---|
| **저장 위치** | 모든 데이터는 내 컴퓨터에 저장됩니다. 설정·DB는 프로젝트의 `.agent-dev/`(또는 관리 폴더), 작업 산출물은 `output/`에 저장됩니다. 서버나 계정이 필요 없습니다. |
| **접속 범위** | 웹 화면은 127.0.0.1에서만 열리고, DevRoster가 열어 준 브라우저(세션 쿠키)에만 응답합니다. 쓰기 요청은 화면 토큰과 같은 출처 검사도 거칩니다. |
| **AI CLI 격리** | AI CLI는 격리된 설정으로 실행됩니다. 개인 `~/.claude`·`~/.codex`·`~/.gemini`의 로그인·플러그인·MCP를 쓰지 않고, 개발 대상 저장소의 `CLAUDE.md`·`AGENTS.md`를 지시로 읽지 않습니다. |
| **비밀값** | API 키·토큰은 권한 0600 파일에만 저장되며 화면·DB·설정 내보내기·승인 snapshot에 들어가지 않습니다. |
| **AI 제공자로 가는 내용** | 기획서, 명세, 역할 지시, 작업에 필요한 코드가 선택한 AI 도구로 전송됩니다. 각 제공자의 데이터 정책을 확인하세요. |

## 문서

| 문서 | 내용 |
|---|---|
| [웹 사용 가이드](docs/web-user-guide.md) | 화면별 사용 순서, 세션 실행, 위키 승인, 슬랙 연동 |
| [설정 가이드](docs/setup-user-guide.md) | 프로젝트·AI·검증·환경변수 설정 |
| [Studio 가이드](docs/studio-user-guide.md) | 기획서 → 명세서 → 목업 → 인계 |
| [다중 AI 가이드](docs/multi-ai-user-guide.md) | CLI·API·로컬 모델 연결 |
| [다른 프로젝트에서 사용하기](docs/reuse.md) | 설치, 초기화, 백업·복원, 프리셋 |
| [실행·이관·복원 가이드](docs/operations.md) | CLI 중심 운영 절차 |
| [REA 가이드](docs/rea-guide.md) | 앱·바이너리 분석 도구 연결 |
| [아키텍처](docs/architecture.md) | 모듈 구조, 실행 흐름, 데이터 모델, 변경 영향 |
| [ADR](docs/adr/) · [설계](docs/design/) | 주요 결정과 기능 설계 |

**처음이라면 이 순서로**

1. [설치](#설치) → [빠른 시작](#빠른-시작)
2. [웹 사용 가이드](docs/web-user-guide.md)로 화면별 순서 익히기
3. [Studio 가이드](docs/studio-user-guide.md)로 기획 → 명세 → 목업
4. [다중 AI 가이드](docs/multi-ai-user-guide.md)로 CLI·API 연결과 모델 선택

## 개발에 참여하기

```sh
.venv/bin/python -m pip install -e ".[dev,ci]"
.venv/bin/python -m pytest -q                 # 전체 테스트
.venv/bin/python scripts/ci_local.py          # CI와 같은 검사(ruff·pyright·테스트·wheel)
```

- **코드 구조**: `agent_dev/domain`(순수 규칙) · `application`(흐름 조정) · `ports`(계약) · `adapters`(파일·DB·프로세스·외부 연동) · `ui`(웹 서버와 화면). 자세한 내용은 [아키텍처](docs/architecture.md)를 참고하세요.
- **실제 AI 호출 확인**: `scripts/smoke_live.py --run --provider codex_cli`(또는 `claude_cli`)로 임시 저장소에서 따로 실행합니다.
- **더 읽을 것**: 개발 환경, 코드 구조, 처음 읽을 파일, PR 규칙은 [기여 안내](CONTRIBUTING.md)에 있습니다.
- **버그 제보와 제안**: GitHub Issues로 부탁드립니다.

## 설치 파일 만들기

PyInstaller는 다른 OS용 파일을 만들지 못하므로 각 OS에서 만듭니다. 보통은 위의 `scripts/build_macos.command`·`scripts\build_windows.bat`이면 충분하고, 다른 사람에게 줄 macOS 디스크 이미지(.dmg)는 직접 만듭니다.

```sh
python3 -m venv .build-venv && .build-venv/bin/pip install -e ".[app]"
.build-venv/bin/python scripts/build_app.py --dmg --smoke     # dist-app/DevRoster-<버전>-macos-<arch>.dmg
```

- 아이콘은 `packaging/DevRoster.png`이고 `python packaging/make_icon.py`로 다시 만들 수 있습니다.

> **주의**: 서명하지 않은 앱이라 다른 컴퓨터에서 받은 파일은 처음 열 때 경고가 나옵니다(macOS: 앱을 **오른쪽 클릭 → 열기**, Windows: **추가 정보 → 실행**).

## 라이선스

[Apache License 2.0](LICENSE)
