# DevRoster 저장소 개요

- DevRoster는 기획서를 바탕으로 여러 AI 도구(Claude·Codex·Gemini CLI, API 도구)로 개발을 진행하는 로컬 앱이다. Python 표준 라이브러리 위에서 동작하고, 웹 화면은 `127.0.0.1`에서만 연다.
- 코드 구조: `agent_dev/domain`(규칙·상태, 외부 접근 없음) → `agent_dev/application` → `agent_dev/adapters`(DB·파일·CLI·HTTP) → `agent_dev/ui`(서버와 `ui/assets` 정적 화면). 바깥 층이 안쪽 층을 부르고, 거꾸로 부르지 않는다.
  - 층 지도: `ports`는 application, `core`·`runners`·`scm`·`storage`·`migration`·`config`·`defaults`·`templates`는 adapters, `app.py`·`entrypoints.py`·`fa.py`는 모든 층을 조립하는 bootstrap이다. 패키지 루트(`agent_dev.__version__`)는 어느 층이든 읽을 수 있다.
  - `tests/test_architecture.py`가 함수 안 import·상대 import·`__import__`까지 검사한다. 새 패키지는 그 파일의 `LAYERS`에 층을 정한다. 다른 모듈의 `_` 이름은 import하지 않는다. 서로를 다시 import하는 순환(함수 안 import 포함)을 새로 만들지 않는다.
  - 남아 있는 위반은 같은 파일의 `KNOWN_*` 목록에 있고 줄어들기만 한다. 위반을 고치면 목록에서 지운다.
- `agent_dev/defaults/`의 AGENTS.md·역할 파일은 **제품이 사용자 프로젝트에 주는 기본 규칙**이다. 이 저장소를 개발하는 규칙(`.ai/rules`)과 섞지 않는다.
- 사용자에게 보이는 문구(화면, 오류, CLI 도움말)는 한국어로 쓴다. 기술 용어·명령·경로는 원문 그대로 둔다.
- 기존 API 계약, 데이터, 권한, 승인 조건, 화면의 DOM id는 바꾸지 않는다. 바꿔야 하면 이유와 영향을 먼저 알린다.
