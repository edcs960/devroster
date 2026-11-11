# 다중 AI 연결·사용 가이드

DevRoster는 CLI 도구와 API 도구를 함께 쓸 수 있다. 이 문서는 어떤 도구를 고르고, 어떻게 연결하며, 모델과 추론 강도를 어떻게 정하는지 다룬다.

## 도구 선택

| 설정 ID | 연결 방식 | 실행 범위 |
|---|---|---|
| `codex_cli` | Codex CLI 로그인/키 | 승인된 파일 읽기·수정, Studio |
| `claude_cli` | Claude CLI 로그인/키 | 승인된 파일 읽기·수정, Studio |
| `gemini_cli` | Gemini CLI 로그인/키 | 승인된 파일 읽기·수정, Studio; shell·MCP·확장·hooks 미연결 |
| `openai_api` | OpenAI Responses API | Studio, 명시적인 `text.generate` 단계 |
| `anthropic_api` | Claude Messages API | Studio, 명시적인 `text.generate` 단계 |
| `gemini_api` | Gemini generateContent API | Studio, 명시적인 `text.generate` 단계 |
| `xai_api` | Grok(xAI) Chat Completions, `XAI_API_KEY` | Studio, 명시적인 `text.generate` 단계 |
| `exaone_api` | LG K-EXAONE Chat Completions, `EXAONE_API_KEY` | Studio, 명시적인 `text.generate` 단계 |
| `compatible_api` | OpenAI 호환 Chat Completions·Ollama | Studio, 명시적인 `text.generate` 단계 |

> **주의**: API 연결은 파일을 직접 읽거나 수정하지 않는다. 실제 개발을 이어가려면 개발 CLI도 활성화한다.

- 기존 프로젝트의 Codex/Claude 설정은 유지하며 새 제공자는 비활성화 상태로 추가된다.
- 자동 모델 선택은 선택한 도구의 등록 모델 안에서 수행한다.

## 웹·터미널 설정

```sh
devroster setup --web --root /path/to/my-project
# 또는 같은 설정을 터미널에서 입력
devroster setup --root /path/to/my-project
```

### 설정 순서

1. **AI 도구**에서 기본 생성 도구를 선택하고 **이 도구 사용**을 켠다.
2. **CLI**: 실행파일 이름/절대 경로와 선택 모델을 입력한다. 로그인은 터미널에서 `codex login`, `claude auth login`, 또는 `gemini`로 진행한다.
3. **API**: API 주소·모델·인증 환경변수 이름·출력 토큰 상한을 입력한다. 모델은 실제 계정이 지원하는 이름을 사용한다.
4. **인증 키 입력란 추가**를 누르고 **환경변수·연동**의 숨겨진 값에 API 키를 입력한다. 인증 없는 로컬 서버는 환경변수 이름을 비운다.
5. **설정 저장** → **환경 진단** 후 Studio를 새로고침한다. **프로젝트의 선택 정책 적용**을 켜면 저장된 고정/자동 정책을 따른다.

### 기본 주소

| API | 기본 주소 | 인증 환경변수 이름 |
|---|---|---|
| **OpenAI** | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| **Claude** | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY` |
| **Gemini** | `https://generativelanguage.googleapis.com/v1beta` | `GEMINI_API_KEY` |
| **K-EXAONE (LG)** | `https://api.exaone.ai/v1` | `EXAONE_API_KEY` |
| **Ollama** | `http://127.0.0.1:11434/v1` | 기본적으로 없음 |

### K-EXAONE

- API 키는 [EXAONE API 플랫폼](https://docs.exaone.ai)에서 발급한다.
- 모델은 `k-exaone_v2.5`가 기본으로 들어 있다.
- 이 모델은 항상 추론한 뒤 답하고, 추론 토큰도 출력 토큰 상한(`max_tokens`)에 포함되므로 기본 상한을 32768로 둔다. 응답이 잘렸다는 오류가 나면 상한을 올린다.
- 추론 강도는 `low`·`high`·`max`만 지원하고(`medium`·`xhigh`는 저장을 거부), 비우면 서비스 기본값 `max`를 쓴다.
- 추론 과정(`reasoning_content`)은 결과에 넣지 않는다.

### Ollama와 호환 API

- Ollama는 먼저 별도 설치하고 서버를 실행한 뒤 모델을 내려받는다. 모델 입력란에는 설치한 모델 이름을 지정한다.
- 원격 호환 API는 서비스가 제공하는 HTTPS base URL과 인증 환경변수 이름을 사용한다.
- URL에는 API 키·계정 정보·쿼리를 넣지 않는다.
- HTTP는 loopback 주소만 허용하며 리다이렉트는 따라가지 않는다.

### 진단의 한계

- API 진단은 설정·키 존재를 확인한다. 유료 생성 없이 실제 인증·모델 접근·서버 상태를 보장할 수는 없다.
- Gemini CLI 로그인 점검 역시 로컬 OAuth 준비 상태/키 존재 확인이며 실제 생성 검증은 아니다.
- Gemini CLI는 `--admin-policy` 지원 버전을 요구하며 중앙 정책이 있으면 작업별 도구 제한을 보장할 수 없어 실행을 거부한다.

## 모델·추론 강도

### 자동 선택 규칙

균형형 자동 선택은 단순 작업 low, 일반 medium, 설계·보안·DB·명세 종합 high를 목표로 한다. 세 작업 등급의 모델과 **모델이 실제 지원하는 강도**를 등록하면 등록 목록과 상한 안에서 선택한다. 지원 목록을 비우면 제공자 기본 강도를 사용하므로 상한을 강제하지 않는다.

### 도구별 추론 옵션

| 도구 | 추론 옵션 |
|---|---|
| **Codex / Claude CLI** | 설치 버전·모델이 지원하는 effort |
| **Gemini CLI** | 도구 기본값만; 고정 강도·지원 강도 목록을 비우고 모델만 선택 |
| **OpenAI API** | 지원 모델의 `reasoning.effort` |
| **Claude API** | 지원 모델의 `output_config.effort`: low/medium/high/max |
| **Gemini API** | 지원 모델의 `thinkingLevel`: low/medium/high; 모델마다 지원 수준이 다름 |
| **호환 API** | 기본 미전달; 지원 서버에서만 `native`로 `reasoning_effort` 전달 |

- 추론을 지원하지 않는 API 모델은 **추론 옵션 전달하지 않음**을 선택하고 강도/지원 목록을 비운다.
- 모든 모델이 같은 옵션을 지원하지 않는다.
- Gemini thinkingLevel은 해당 기능을 지원하는 모델에서 사용하며 예전 모델의 thinkingBudget 자동 변환은 하지 않는다.
- 자동 선택 이유는 Studio 최근 생성·변경 이력과 workflow의 `RunnerUsage.selection`에 기록된다.

```sh
devroster setup --preview-task "권한 설계" --preview-phase security --root /path/to/my-project
devroster setup --show --root /path/to/my-project
devroster setup --check --root /path/to/my-project
```

## 저장·실패·복구

**키와 설정의 저장**

- API 키 값은 프로젝트의 `.agent-dev/environment.json`에 비공개 저장한다.
- 프로필/snapshot/내보내기/백업에는 키 이름만 포함된다.
- 복원·다른 프로젝트 복사 후 키 값을 다시 입력한다.
- 이미 승인한 작업과 생성 중인 작업은 설정 변경 영향을 받지 않는다. 새 설정은 다음 승인·생성부터 적용한다.

**오류별 확인**

| 증상 | 확인 |
|---|---|
| 401/403 | 인증 키·권한 확인 |
| 429 | 사용 한도 확인 |
| 모델/옵션 오류 | 계정 모델 이름·추론 지원 확인 |
| 빈 응답/잘림 | 모델 및 출력 토큰 상한 확인 |

**결과 처리**

- 응답이 정상 JSON이어도 명세·목업 검증을 통과해야 저장된다.
- 실패·중단 시 이전 명세서와 목업을 유지한다. Studio는 자동 유료 재시도하지 않는다.
- 중단·프로젝트 버전 변경 뒤 늦게 도착한 API 응답은 결과에 반영하지 않는다. 원격 요청의 실제 중단/과금 취소는 제공자가 결정한다.
- 공통 `text.generate` workflow에는 기존 일시 오류 재시도·사용량 cooldown·capability 선택을 적용한다.
- API를 파일 수정 단계의 fallback으로 사용하지 않는다.

공식 계약: [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning), [Claude Messages](https://platform.claude.com/docs/en/api/messages/create), [Gemini thinking](https://ai.google.dev/gemini-api/docs/thinking), [Gemini CLI policy](https://geminicli.com/docs/reference/policy-engine/), [Ollama 호환 API](https://docs.ollama.com/api/openai-compatibility).
