# DevRoster에 기여하기

DevRoster는 Python 표준 라이브러리만으로 동작하는 로컬 앱입니다. 실행에 필요한 외부 패키지가 없고, 웹 화면은 `127.0.0.1`에서만 엽니다. 이 문서는 처음 코드를 보는 분이 어디서부터 읽고, 어떻게 확인하고, 어떻게 PR을 보내면 되는지 안내합니다.

**목차**

- [개발 환경](#개발-환경)
- [확인 명령](#확인-명령)
- [코드 구조](#코드-구조)
- [처음 읽을 파일](#처음-읽을-파일)
- [용어](#용어)
- [화면을 바꿀 때](#화면을-바꿀-때)
- [PR 보내기](#pr-보내기)

## 개발 환경

```sh
git clone https://github.com/edcs960/devroster.git
cd devroster
python3 -m venv .venv                      # Python 3.11 이상
.venv/bin/python -m pip install -e ".[dev,ci]"
```

## 확인 명령

| 확인 | 명령 |
|---|---|
| **전체 테스트** | `.venv/bin/python -m pytest -q -p no:cacheprovider` |
| **lint** | `.venv/bin/ruff check agent_dev tests scripts` |
| **타입** | `.venv/bin/pyright --pythonplatform Linux <범위>`. 범위는 `.github/workflows/refactor-quality.yml`에 적힌 그대로 씁니다. ruff·pyright·build 버전은 `pyproject.toml`의 `ci` 묶음에 고정되어 있습니다. |
| **CI와 같은 순서로 한 번에** | `python3 scripts/ci_local.py --python .venv/bin/python` |
| **브라우저 테스트** | `AGENT_DEV_TEST_PLAYWRIGHT_MODULE`(Playwright 모듈 경로)과 `AGENT_DEV_TEST_BROWSER_EXECUTABLE`(Chrome 실행 파일)을 지정하면 실행되고, 없으면 건너뜁니다. 화면을 바꿨다면 지정해서 실행해 주세요. |

> **주의**: CI는 Linux·macOS·Windows에서 돕니다. 파일·프로세스 입출력에는 항상 `encoding="utf-8"`을 지정해 주세요(`tests/test_text_encoding.py`가 검사합니다).

## 코드 구조

바깥 층이 안쪽 층을 부르고, 거꾸로 부르지 않습니다. `tests/test_workflow_runtime.py`가 `application` 층의 의존을 검사합니다.

| 층 | 폴더 | 하는 일 |
|---|---|---|
| **도메인** | `agent_dev/domain` | 규칙과 상태. 파일·DB·네트워크에 접근하지 않습니다. |
| **계약** | `agent_dev/ports` | 바깥 층이 구현하는 인터페이스 |
| **흐름** | `agent_dev/application` | 작업 실행 흐름(단계식, 워크플로 그래프) |
| **연결** | `agent_dev/adapters` | SQLite, 파일, Git, AI CLI, HTTP |
| **화면** | `agent_dev/ui` | 로컬 웹 서버와 `ui/assets` 정적 화면 |

`agent_dev/defaults/`의 AGENTS.md·역할 파일은 제품이 **사용자 프로젝트에** 주는 기본 규칙입니다. 이 저장소를 개발하는 규칙(`.ai/rules`)과는 다릅니다.

## 처음 읽을 파일

1. `agent_dev/domain/transitions.py`: 작업 상태와 허용된 전이
2. `agent_dev/application/workflow_dag.py`: 승인된 스냅샷으로 역할 노드를 실행하는 흐름
3. `agent_dev/adapters/harness.py`: AI 결과에서 읽는 유일한 제어 형식(Harness Control JSON)
4. `agent_dev/adapters/artifacts.py`: 역할마다 AI에게 주는 입력(프롬프트 맥락)
5. `agent_dev/ui/studio_server.py`: 웹 화면과 API의 진입점

더 자세한 설명은 [아키텍처](docs/architecture.md)와 [ADR](docs/adr/)에 있습니다.

## 용어

| 용어 | 뜻 |
|---|---|
| **스냅샷** | 계획 승인 시점의 설정·설계·역할·권한을 고정한 기록입니다. 실행은 항상 스냅샷을 따르고, 설정을 바꿔도 이미 승인된 작업에는 영향이 없습니다. |
| **역할 정책** | 역할마다 읽기 전용인지, 어느 경로를 쓸 수 있는지 정한 규칙입니다. AI가 끝난 뒤 실제 파일 변경과 대조합니다. |
| **Harness Control** | AI 결과 끝에 붙는 JSON 블록(`handoff_status` 등)입니다. 이 블록만 다음 단계 결정에 씁니다. |
| **게이트** | 프로젝트 설정의 검증 명령(테스트·lint)입니다. AI가 아니라 DevRoster가 실행합니다. |
| **작업 폴더(워크트리)** | 작업마다 만드는 Git worktree입니다. 사용자의 원래 체크아웃에는 쓰지 않습니다. |

## 화면을 바꿀 때

- 보안 정책(CSP)상 인라인 `<script>`, `style=""`, `data:` URI를 쓰지 않습니다.
- 알림·확인·메뉴는 `ui-feedback.js`의 `DevRosterUI`를 씁니다.
- 1440·1024·768·390px 폭에서 가로 넘침이 없는지, 키보드로 조작되는지 확인해 주세요.

## PR 보내기

- 기능마다 브랜치를 만들어 PR을 보내 주세요.
- 커밋 메시지는 한국어로 씁니다. 제목은 `feat:`·`fix:`·`docs:`·`chore:` 뒤에 무엇을 바꿨는지, 본문에는 왜 바꿨는지와 검증 결과를 씁니다.
- 개인 경로, 이메일, 비밀값을 커밋하지 마세요.
- 실행하지 못한 확인이 있으면 PR 설명에 이유와 함께 적어 주세요.
- 사용자에게 보이는 문구(화면, 오류, CLI 도움말)는 한국어로 씁니다.

버그 제보와 제안은 GitHub Issues로 부탁드립니다.
