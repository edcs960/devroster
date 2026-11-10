# 검증

- 전체 테스트: `.venv/bin/python -m pytest -q -p no:cacheprovider`
- 브라우저 테스트는 `AGENT_DEV_TEST_PLAYWRIGHT_MODULE`(Playwright 모듈 경로)과 `AGENT_DEV_TEST_BROWSER_EXECUTABLE`(Chrome 실행 파일)을 지정해야 실행되고, 없으면 건너뛴다. 화면을 바꿨다면 지정해서 실행한다.
- lint는 `ruff check agent_dev tests scripts`, 타입 검사는 `.github/workflows/`에 적힌 범위 그대로 `pyright --pythonplatform Linux`를 실행한다. 도구 버전은 `pyproject.toml`의 `ci` 묶음에 고정되어 있고, 올릴 때는 그 묶음만 바꾼다.
- 파일·프로세스 입출력은 항상 `encoding="utf-8"`을 지정한다(Windows 기본 인코딩 때문). `tests/test_text_encoding.py`가 이를 검사한다.
- CI는 Linux·macOS·Windows에서 돈다. 경로 구분자, 줄바꿈, 소켓 동작이 플랫폼마다 다를 수 있음을 고려한다.
- 실행하지 못한 검증은 실행하지 못했다고 이유와 함께 적는다.
