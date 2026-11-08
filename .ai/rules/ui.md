# 화면 (agent_dev/ui/assets)

- 보안 정책(CSP)이 `default-src 'self'; script-src 'self'; style-src 'self'`다. 인라인 `<script>`·`style=""`·`data:` URI를 쓰지 않는다. 아이콘은 `brand-mark.svg`의 `nav-*` 심볼을 쓴다.
- 새 정적 파일은 `agent_dev/ui/shared_assets.py` 허용 목록과 `pyproject.toml` package-data에 함께 등록한다.
- 색·간격·크기는 `ui-tokens.css`의 `--c-*`, `--sp-*`, `--r-*`, `--h-*`, `--fs-*` 토큰을 쓴다. 공통 스타일은 `ui-base.css`·`ui-components.css`에 둔다.
- 알림·확인·메뉴는 `ui-feedback.js`의 `DevRosterUI`(toast, confirmAction, menu, busy, errorMessage)를 쓴다. `alert`·`confirm`을 새로 쓰지 않는다.
- 피드백 규칙: 성공은 토스트, 입력 오류는 필드 아래, 막히는 오류는 해결 행동이 있는 배너, 위험한 작업은 확인 대화상자.
- 화면을 바꾸면 1440·1024·768·390px 폭에서 가로 넘침이 없는지, 키보드로 조작되는지, `prefers-reduced-motion`을 지키는지 확인한다.
