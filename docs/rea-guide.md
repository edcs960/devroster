# REA 사용 가이드

REA(Reverse Engineer Anything)는 이미 만들어진 앱·바이너리가 실제로 어떻게 구성됐는지 로컬에서 분석하는 MCP 서버다. DevRoster에서는 개발 흐름의 **도구 단계**로 실행하고, 분석 결과를 다음 역할(Architect·Developer·QA 등)에게 참고 자료로 넘긴다.

- 공식 저장소: <https://github.com/morluto/rea> (MIT, npm `rea-agents`)
- 기준: REA 6.8.0, DevRoster `main` (PR #38 이후)

## 1. 언제 쓰는가

| 상황 | 예 |
|---|---|
| 문서 없는 기존 앱과 연동 | 설치된 Electron 앱이 어떤 모듈·IPC로 구성됐는지 파악한 뒤 연동 기능 설계 |
| 네이티브 라이브러리 연동 | `.so`·`.dll`이 내보내는 함수 목록 확인 |
| 배포본 의존성 점검 | Go·Rust 실행 파일에 들어간 의존성·빌드 정보 확인 |
| Windows 실행 파일 점검 | `.exe`가 불러오는 DLL·함수 확인 |

소스가 있는 코드는 소스 탐색·코드 그래프로 충분하다. REA는 **소스 없이 결과물만 있을 때** 쓴다.

## 2. 준비

1. Node 버전: 22.23.4 이상(22.x), 24.22.1 이상(24.x), 26.12.1 이상(26.x) 중 하나. `node -v`로 확인한다.
2. 설치: `npm install --global rea-agents` → `rea --help`로 확인.
   - DevRoster는 MCP 서버를 `npx`·`npm`으로 실행하지 않는다(실행할 때마다 다른 버전을 받을 수 있어서). 전역 설치로 `rea` 실행 파일이 PATH에 있어야 한다.
3. 이번에 허용한 도구는 외부 엔진이 필요 없다. Ghidra·Hopper·IDA 같은 엔진은 설치하지 않아도 된다.

`npx rea-agents setup`은 Claude Code·Codex 같은 **개인 AI 도구**에 REA를 직접 등록하는 명령이다. DevRoster는 실행할 때 AI 도구의 개인 MCP 설정을 끄므로, DevRoster 안에서 쓰려면 아래 연결 절차를 따른다.

## 3. 연결 등록

### 웹

1. 메뉴 **설정·자료 → 외부 도구 연결**(`/integrations`)을 연다.
2. 카탈로그 **앱·바이너리 분석** 묶음에서 **REA (Reverse Engineer Anything)** 를 고른다.
3. 연결 ID(예: `rea`)와 범위 이름(예: `legacy-app`)을 넣고 저장한다. 범위 이름은 이 연결을 어떤 분석에 쓸지 구분하는 이름이며, 도구 단계에서 같은 이름을 고른다.
4. **연결 검사**를 누른다. `rea`를 찾지 못하면 "MCP 실행 파일을 먼저 설치해주세요"가 나온다.

### 터미널

```json
{"id": "rea", "provider": "rea", "scopes": ["legacy-app"]}
```

```sh
devroster integration configure rea.json --root /path/to/project
devroster integration check rea --root /path/to/project
```

## 4. 개발 흐름에 도구 단계 넣기

1. 메뉴 **개발 흐름**(`/workflows`)에서 분석 결과가 필요한 역할 **앞**에 도구 단계를 추가한다. 보통 Planner 다음, Architect 앞이다.
2. 단계 설정:

| 항목 | 값 |
|---|---|
| 연결 | `rea` |
| 동작 | 아래 표의 도구 이름 |
| 권한(capability) | `analysis.read` |
| 범위 | 연결에 넣은 이름(예: `legacy-app`) |
| 입력값(arguments) | 분석할 파일·폴더의 **절대 경로** |
| 필수 여부 | 분석이 없어도 진행해도 되면 끈다(실패해도 건너뜀으로 기록) |

3. **프로젝트에 적용**한다. 이미 승인한 작업에는 바뀌지 않고, **다음에 승인하는 작업**부터 이 단계가 들어간다.

고급 설정 JSON 예:

```json
{
  "id": "inspect-legacy-app",
  "kind": "tool",
  "connection_id": "rea",
  "operation": "analyze_javascript_application",
  "capability": "analysis.read",
  "scope": "legacy-app",
  "arguments": {"input_path": "/Applications/Example.app/Contents/Resources/app.asar"},
  "required": false,
  "budget": {"max_calls": 2, "timeout_seconds": 600}
}
```

## 5. 쓸 수 있는 동작

도구 단계는 **고정된 입력값으로 한 번 호출**한다. 그래서 로컬 경로 하나만으로 끝나는 분석 도구만 허용했다.

| 동작 | 필수 입력 | 하는 일 |
|---|---|---|
| `analyze_javascript_application` | `input_path`: ASAR 또는 풀어 둔 JS 앱 폴더 | 앱을 실행하지 않고 모듈·연결 관계를 복원. 분석 중 임시 파일을 쓰고 보조 프로세스를 띄울 수 있다 |
| `inspect_desktop_application` | `path`: 데스크톱 앱 폴더 | 파일 목록·헤더·메타데이터로 앱 구성 파악 |
| `inspect_native_exports` | `path`: PE(.dll/.exe) 또는 ELF(.so) | 내보내는 함수·심볼 목록 |
| `inspect_pe_imports` | `path`: Windows PE | 불러오는 DLL·함수(지연 로드 포함) |
| `inspect_go_binary` | `path`: Go 실행 파일 | Go 버전·메인 모듈·의존성·빌드 설정 |
| `inspect_rust_binary` | `path`: Rust 실행 파일 | cargo-auditable 의존성 목록 |

일부러 제외한 것:

- 엔진 세션이 먼저 열려 있어야 하는 도구(예: `binary_overview`)
- 실행 중인 브라우저 연결이 필요한 도구(예: `analyze_web_bundle`)
- 앞선 분석 결과를 입력으로 받는 도구(예: `compare_application_versions`)
- 기기 조작(adb), 실행 중 계측(Frida), 디버거(gdb·rizin), 명령 실행 도구

이 도구들이 필요하면 DevRoster 밖에서 개인 AI 도구에 REA를 등록해 대화형으로 쓴다.

## 6. 결과가 전달되는 방식

- 도구 단계가 통과하면 결과는 이후 모든 역할의 실행 입력에 `tool_evidence_untrusted`로 들어간다. 역할은 이를 **참고 데이터**로만 다루며 지시로 따르지 않는다.
- 호출 기록(`WorkflowToolCall`)과 결과는 작업 기록에 남는다. 같은 단계의 호출 수는 `budget.max_calls`(기본 10)로 제한된다.

## 7. 주의

- **라이선스·약관**: 분석 대상의 라이선스와 이용 약관이 역공학을 허용하는지 먼저 확인한다. 자기 조직의 결과물이나 연동 권한이 있는 대상에 쓴다.
- **데이터 전송**: 분석은 로컬에서 하지만, 결과가 AI 역할의 입력에 들어가면 그 AI 제공자에게 전송된다. 민감한 바이너리라면 도구 단계 뒤 역할이 쓰는 AI 도구의 데이터 정책을 확인한다.
- **경로**: 입력 경로는 절대 경로로 쓴다. 여러 사람이 같은 개발 흐름을 쓰면 경로가 컴퓨터마다 다를 수 있다.

## 8. 문제 해결

| 증상 | 확인 |
|---|---|
| 연결 검사: "MCP 실행 파일을 먼저 설치해주세요" | `which rea`. 없으면 `npm install --global rea-agents` |
| 설치는 됐는데 실행 실패 | `node -v`가 2절 버전 조건을 만족하는지 |
| 작업이 "Workflow tool exceeds snapshot … grant"로 멈춤 | 도구 단계의 연결·동작·범위·권한이 연결 설정과 같은지, 연결을 등록한 뒤 작업을 **새로 승인**했는지 |
| "TOOL_CALL_LIMIT_REACHED" | 단계의 `budget.max_calls`를 늘리거나 재시도를 줄인다 |
| 결과가 비어 있음 | 입력 경로가 그 도구가 받는 형식(ASAR, PE, ELF, Go/Rust 실행 파일)인지 |
