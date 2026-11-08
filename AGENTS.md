<!-- devroster:ai begin — .ai/에서 생성됨. 이 사이는 직접 고치지 마세요 (devroster rules sync) -->
# `.ai/rules/git-release.md`

# 커밋과 공개

- 이 저장소는 개발용(private)이고, 검증한 상태만 공개 저장소에 릴리스 커밋으로 반영한다.
- 기본 브랜치에 직접 커밋하지 않는다. 기능마다 브랜치를 만들고, 커밋·푸시는 사용자가 요청할 때만 한다.
- 추적되는 파일에 개인 경로(사용자 홈 폴더 이름), 개인 이메일, 사내 프로젝트 이름, 비밀값을 넣지 않는다. 공개 반영 스크립트가 이를 검사하고 발견하면 중단한다.
- 버전을 올릴 때는 `pyproject.toml`, `agent_dev/__init__.py`, MCP 클라이언트 정보, 설치 문서의 버전을 함께 바꾼다. 작업 공간 형식 표시값(`engine_version`)은 형식이 바뀔 때만 바꾼다.
- 커밋 메시지는 `feat:`·`fix:`·`docs:`·`chore:` 접두어와 변경 이유를 쓴다.

# `.ai/rules/project.md`

# DevRoster 저장소 개요

- DevRoster는 기획서를 바탕으로 여러 AI 도구(Claude·Codex·Gemini CLI, API 도구)로 개발을 진행하는 로컬 앱이다. Python 표준 라이브러리 위에서 동작하고, 웹 화면은 `127.0.0.1`에서만 연다.
- 코드 구조: `agent_dev/domain`(규칙·상태, 외부 접근 없음) → `agent_dev/application` → `agent_dev/adapters`(DB·파일·CLI·HTTP) → `agent_dev/ui`(서버와 `ui/assets` 정적 화면). 바깥 층이 안쪽 층을 부르고, 거꾸로 부르지 않는다.
- `agent_dev/defaults/`의 AGENTS.md·역할 파일은 **제품이 사용자 프로젝트에 주는 기본 규칙**이다. 이 저장소를 개발하는 규칙(`.ai/rules`)과 섞지 않는다.
- 사용자에게 보이는 문구(화면, 오류, CLI 도움말)는 한국어로 쓴다. 기술 용어·명령·경로는 원문 그대로 둔다.
- 기존 API 계약, 데이터, 권한, 승인 조건, 화면의 DOM id는 바꾸지 않는다. 바꿔야 하면 이유와 영향을 먼저 알린다.

# `.ai/rules/testing.md`

# 검증

- 전체 테스트: `.venv/bin/python -m pytest -q -p no:cacheprovider`
- 브라우저 테스트는 `AGENT_DEV_TEST_PLAYWRIGHT_MODULE`(Playwright 모듈 경로)과 `AGENT_DEV_TEST_BROWSER_EXECUTABLE`(Chrome 실행 파일)을 지정해야 실행되고, 없으면 건너뛴다. 화면을 바꿨다면 지정해서 실행한다.
- lint·타입 검사는 `.github/workflows/`에 적힌 범위 그대로 `ruff check`와 `pyright --pythonplatform Linux`를 실행한다.
- 파일·프로세스 입출력은 항상 `encoding="utf-8"`을 지정한다(Windows 기본 인코딩 때문). `tests/test_text_encoding.py`가 이를 검사한다.
- CI는 Linux·macOS·Windows에서 돈다. 경로 구분자, 줄바꿈, 소켓 동작이 플랫폼마다 다를 수 있음을 고려한다.
- 실행하지 못한 검증은 실행하지 못했다고 이유와 함께 적는다.

# `.ai/rules/ui.md`

# 화면 (agent_dev/ui/assets)

- 보안 정책(CSP)이 `default-src 'self'; script-src 'self'; style-src 'self'`다. 인라인 `<script>`·`style=""`·`data:` URI를 쓰지 않는다. 아이콘은 `brand-mark.svg`의 `nav-*` 심볼을 쓴다.
- 새 정적 파일은 `agent_dev/ui/shared_assets.py` 허용 목록과 `pyproject.toml` package-data에 함께 등록한다.
- 색·간격·크기는 `ui-tokens.css`의 `--c-*`, `--sp-*`, `--r-*`, `--h-*`, `--fs-*` 토큰을 쓴다. 공통 스타일은 `ui-base.css`·`ui-components.css`에 둔다.
- 알림·확인·메뉴는 `ui-feedback.js`의 `DevRosterUI`(toast, confirmAction, menu, busy, errorMessage)를 쓴다. `alert`·`confirm`을 새로 쓰지 않는다.
- 피드백 규칙: 성공은 토스트, 입력 오류는 필드 아래, 막히는 오류는 해결 행동이 있는 배너, 위험한 작업은 확인 대화상자.
- 화면을 바꾸면 1440·1024·768·390px 폭에서 가로 넘침이 없는지, 키보드로 조작되는지, `prefers-reduced-motion`을 지키는지 확인한다.
<!-- devroster:ai end -->
