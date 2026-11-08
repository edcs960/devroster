# DevRoster 저장소 개요

- DevRoster는 기획서를 바탕으로 여러 AI 도구(Claude·Codex·Gemini CLI, API 도구)로 개발을 진행하는 로컬 앱이다. Python 표준 라이브러리 위에서 동작하고, 웹 화면은 `127.0.0.1`에서만 연다.
- 코드 구조: `agent_dev/domain`(규칙·상태, 외부 접근 없음) → `agent_dev/application` → `agent_dev/adapters`(DB·파일·CLI·HTTP) → `agent_dev/ui`(서버와 `ui/assets` 정적 화면). 바깥 층이 안쪽 층을 부르고, 거꾸로 부르지 않는다.
- `agent_dev/defaults/`의 AGENTS.md·역할 파일은 **제품이 사용자 프로젝트에 주는 기본 규칙**이다. 이 저장소를 개발하는 규칙(`.ai/rules`)과 섞지 않는다.
- 사용자에게 보이는 문구(화면, 오류, CLI 도움말)는 한국어로 쓴다. 기술 용어·명령·경로는 원문 그대로 둔다.
- 기존 API 계약, 데이터, 권한, 승인 조건, 화면의 DOM id는 바꾸지 않는다. 바꿔야 하면 이유와 영향을 먼저 알린다.
