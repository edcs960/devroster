# 개발 Workflow MCP 통합 카탈로그 설계

- 날짜: 2026-10-06
- 상태: 구현·로컬 검증 완료; 외부 계정/실설치 별도 검증; 런타임 통합/신규 설치/외부 계정 인증 완료 목록이 아님.
- 기준: 공식 제공자 문서/저장소 확인, 프로젝트 개발 단계에 실질적으로 필요한 기능, 기존 기능과 중복되는 실행은 선택적으로 사용.
- 연결은 프로젝트별로 선택한다. 기본 카탈로그 등록과 실제 사용 활성화는 별개이며 역할/노드에 필요한 도구만 조회한다.

## 1. 카탈로그: MCP 서버 20종 + 선택적 Gateway 1종

| ID | 제공자 | 용도 / 단계 | 연결 방식 / 인증 | 기본 동작 | 공식 근거 |
|---|---|---|---|---|---|
| context7 | Context7 | 라이브러리 버전별 문서, Architect/Developer | HTTP `https://mcp.context7.com/mcp` API key/제한된 anonymous; OAuth 별도 endpoint, 기존 stdio도 허용 | docs.read | [설치/인증](https://context7.com/docs/resources/all-clients) |
| github | GitHub | 저장소/Issue/PR/Actions, Planner/Developer/Reviewer/Infra | HTTP `https://api.githubcopilot.com/mcp/readonly`, OAuth/PAT; 공식 local binary도 선택 | github.read; PR/커밋/머지는 기존 publishing 경로 | [공식 서버 설정](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md) |
| stitch | Stitch | 화면 참고, Planner/UX/Designer/Frontend/QA | 기존 HTTP endpoint + private API key | design.read, 읽기4메서드 | 기존 실측/상위 설계 §7.1 |
| figma | Figma | 프레임/컴포넌트/토큰/Code Connect 참고, UX/Designer/Frontend/QA | 지원 MCP client OAuth; standalone REST PAT fallback은 별도 어댑터 | design.read | [공식 MCP](https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/) |
| playwright | Playwright | 실제 로컬 화면 조작·E2E·스크린샷, QA/Test/Security | stdio, 공식 `@playwright/mcp`, 설치된 pinned version/별도 테스트 browser | browser.test, 등록된 local/dev URL만 | [공식 서버](https://github.com/microsoft/playwright-mcp) |
| chrome-devtools | Chrome DevTools | 네트워크/console/performance trace, Frontend/QA/Infra | stdio `chrome-devtools-mcp`, Node/Chrome 필요 | browser.inspect; 프로젝트 테스트 browser만 | [공식 서버](https://github.com/ChromeDevTools/chrome-devtools-mcp) |
| notion | Notion | 기획서/요구사항/팀 문서 가져오기, Planner/Architect/Writer | HTTP `https://mcp.notion.com/mcp`, OAuth | docs.read, 사용자가 공유한 workspace/page | [공식 연결](https://developers.notion.com/guides/mcp/get-started-with-mcp) |
| linear | Linear | Issue/프로젝트 요구사항·진행 조회, Planner/QA/Reviewer | HTTP `https://mcp.linear.app/mcp/readonly`, OAuth/API key | tasks.read | [공식 MCP](https://linear.app/docs/mcp) |
| atlassian | Atlassian Jira/Confluence | Issue/기준 문서/팀 지식, Planner/Architect/Writer | HTTP `https://mcp.atlassian.com/v2/mcp`, OAuth 또는 공식 API token 방식 | tasks.read/docs.read, project/space 범위 | [공식 연결](https://support.atlassian.com/atlassian-ai-gateway/docs/get-started-with-the-atlassian-remote-mcp-server/) |
| dbhub | DBHub | Postgres/MySQL/SQL Server/MariaDB/SQLite 메타데이터·읽기 조회, DBA/Backend | 로컬 stdio/등록 loopback HTTP, private DSN + DB read-only 계정 | database.inspect/database.query, row/time 한도 | [공식 서버](https://github.com/bytebase/dbhub) |
| supabase | Supabase | 스키마/프로젝트 로그/문서, DBA/Backend/Infra | HTTP `https://mcp.supabase.com/mcp`, 공식 auth; project_ref 고정 + read_only=true + feature 제한 | database.inspect/database.query/platform.read | [공식 MCP](https://supabase.com/docs/guides/ai-tools/mcp) |
| neon | Neon | Postgres schema/query/DB 진단, DBA/Backend | HTTP `https://mcp.neon.tech/mcp`, OAuth/API key; readonly=true/projectId/category 제한 | database.inspect/database.query | [공식 MCP](https://neon.com/docs/ai/neon-mcp-server) |
| sentry | Sentry | 실제 오류/event/성능/회귀 원인, Developer/QA/Infra | HTTP `https://mcp.sentry.dev/mcp`, OAuth; organization/project 범위 URL | observability.read | [공식 MCP](https://mcp.sentry.dev/) |
| snyk | Snyk | 코드·의존성·IaC/컨테이너 취약점 검사, Security/QA | stdio, 설치된 `snyk mcp -t stdio`, CLI auth/private token | security.sast/security.sca; 승인된 worktree와 조직 | [공식 서버 안내](https://snyk.io/articles/snyk-mcp-cheat-sheet/) |
| vercel | Vercel | 문서/프로젝트/배포 로그·상태, Infra/Reviewer | HTTP `https://mcp.vercel.com`, OAuth/지원 client 확인; standalone API는 별도 capability | deployment.read | [공식 MCP](https://vercel.com/docs/agent-resources/vercel-mcp) |
| railway | Railway | 서비스/환경/배포 로그·상태, Infra/Reviewer | HTTP `https://mcp.railway.com` OAuth 또는 설치된 `railway mcp` stdio/CLI auth | deployment.read; 환경변수는 이름/설정 존재만 표시 | [공식 MCP](https://docs.railway.com/ai/mcp-server) |
| firecrawl | Firecrawl | 외부 기획 참고·공식 기술문서 가져오기, Planner/Architect | HTTP `https://mcp.firecrawl.dev/v2/mcp` key 또는 별도 OAuth endpoint | web.read, URL/검색 범위 및 usage 제한 | [공식 MCP](https://docs.firecrawl.dev/mcp-server) |
| serena | Serena | 함수·클래스·참조 단위 코드 조회, Architect/Developer/Reviewer | 설치된 고정 버전 `serena start-mcp-server` stdio, 프로젝트별 프로세스/언어 서버 | code.read; 편집·쉘 실행 기본 제외 | [공식 서버](https://github.com/oraios/serena), [설정](https://oraios.github.io/serena/02-usage/020_running.html) |
| context-mode | Context Mode | 대용량 자료 색인/필요 부분 검색, Developer/QA/Writer | 설치된 고정 버전 `context-mode` stdio, 실행환경 점검 | context.index/search/stats; 실행·업그레이드·삭제 도구/전역 hook 자동 활성화 제외 | [공식 서버](https://github.com/mksglu/context-mode) |
| graphify | Graphify | 프로젝트 관계·경로·변경 영향 탐색, Architect/Developer/Reviewer/QA | 공식 `graphifyy[mcp]`의 고정 버전/격리 환경, 해당 환경의 Python으로 `-m graphify.serve <graph.json>` stdio | graph.read; 지정한 프로젝트 graph의 query_graph/get_node/get_neighbors/shortest_path | [공식 MCP 안내](https://github.com/Graphify-Labs/graphify#using-the-graph-directly), [서버 구현](https://github.com/Graphify-Labs/graphify/blob/v8/graphify/serve.py) |
| rea | REA (Reverse Engineer Anything) | 기존 앱·웹 번들·바이너리 동작 분석(문서가 없는 레거시 연동, 버전 비교), Architect/Developer/QA | npm `rea-agents`의 `rea mcp` stdio(Node 22.23.4+/24.22.1+/26.12.1+). 네이티브 분석은 Ghidra 등 외부 엔진이 따로 필요 | analysis.read; 로컬 경로 하나로 끝나는 분석 6개(analyze_javascript_application·inspect_desktop_application·inspect_native_exports·inspect_pe_imports·inspect_go_binary·inspect_rust_binary). 엔진 세션·실행 중 브라우저·앞선 결과가 필요한 도구와 기기 조작·계측(Frida)·디버거·명령 실행 도구 제외. 사용법: [REA 사용 가이드](rea-guide.md). 분석 대상의 라이선스·이용 약관은 사용자가 확인 | [공식 저장소](https://github.com/morluto/rea) |
| docker-gateway | Docker MCP Gateway (선택) | 로컬 MCP 서버 관리·컨테이너 실행 경계, Infra/설정 | 설치된 Docker MCP Toolkit/Gateway, 등록 catalog/profile만 | 전송 브리지; 모든 컨테이너 관리 권한은 부여하지 않음 | [공식 Gateway](https://docs.docker.com/ai/mcp-catalog-and-toolkit/mcp-gateway/) |

21종 server와 1종 bridge는 전부 기본 카탈로그에 제공하되 필요할 때 프로젝트에서 연결한다. 외부 서비스의 프로젝트/요금제/권한을 갖고 있어야 해당 기능이 실제로 동작한다. Microsoft/Google/협업 connector를 MCP 서버라고 오표시하지 않는다. REST fallback은 transport=api로 명시한다.

## 2. 현재 설치·인증·검증 상태

| 구분 | 현재 확인 | 의미 |
|---|---|---|
| Codex CLI 등록 | context7/github/stitch/figma/playwright enabled | 프로젝트 새 런타임 통합 또는 실제 인증 완료 보장 아님 |
| Claude health | context7/stitch/playwright Connected, GitHub Failed | Claude 등록 경로별 현재 health 결과 |
| Stitch 실제 호출 | 프로젝트2/화면82/개별1/HTML·PNG200 | 이전 진단의 읽기 성공; 생성/수정은 호출하지 않음 |
| Figma | MCP 등록만 확인, file 링크 미제공 | 실제 file 접근 Not Tested |
| 새 서버15종/선택 Gateway | 설계 카탈로그만 추가 | 설치/계정 연결/실제 조회 Not Tested |
| Plugin Management 검색 | Linear/Notion/Atlassian/Supabase/Neon available, installed=false | ChatGPT plugin 발견 상태이며 제품 MCP 연결 상태와 별개 |
| 현재 앱 도구 | GitHub/Figma/Vercel/Railway 도구 노출 | 특정 프로젝트 실행기의 등록·인증·scope로 자동 전파하지 않음 |

## 3. 공통 등록·연결·호출 계약

- 제공자 카탈로그는 id/이름/설명/공식문서/transport/auth/필요binary/역할추천/default capabilities를 갖는다. 설치된 패키지/binary 버전과 tool schema digest는 연결 점검 후 별도 lock record로 저장한다.
- 등록 연결은 `connection_id/project_id/provider_id/transport/endpoint 또는 command+args/credential_ref/allowed_scopes/capabilities/enabled/policy_version`으로 구성한다. credential value와 DSN은 private 영역에만 저장한다. 연결 ID는 전체 DB에서 유일하며 프로젝트 소유권을 바꾸는 upsert를 거부한다.
- 연결 상태: catalog_only/configured/installation_required/auth_required/checking/ready/partial/error/disabled. ready는 실제 허용 도구 호출 성공 근거/시각/scope가 있어야 한다. 과거 성공 결과와 현재 연결 점검을 혼동하지 않는다.
- HTTP transport: initialize→initialized→tools/list(pagination)→허용된 tools/call, MCP protocol negotiation, JSON/SSE, timeout/response limit, 세션 종료 지원. 인증 refresh는 scope를 늘리지 않는다.
- OAuth: 제공자의 공식 protected resource/authorization metadata를 확인하고 PKCE/state/loopback callback으로 앱의 연결을 승인한다. 지원 client 제한이 있는 Figma/Vercel은 승인된 client 수집 경로 또는 명시적인 REST 대체 capability를 사용한다. 타 client token을 추출하거나 미지원 provider의 OAuth를 API key로 대체했다고 표시하지 않는다.
- stdio: 이미 설치된 등록 executable을 argv 배열/shell=False/최소 env/고정 cwd로 실행한다. 프로세스 그룹·lease·취소·timeout·stderr redaction을 적용한다. 아직 설치되지 않은 서버를 tool call 중 `npx -y @latest`로 내려받아 실행하지 않는다. 실행기 이름 목록(npx·uvx·pip·npm·curl·sh·bash)은 안내일 뿐 경계가 아니다. 경계는 실행 확인이다: 해석된 실행 파일 경로·argv·환경변수 이름을 사용자가 확인한 뒤에만 시작하고, 셋 중 하나라도 바뀌면 다시 확인한다(`Integrations.launch`/`approve_launch`, 확인은 관리 폴더의 `workspace-trust.json`).
- 웹 `설치 준비`는 공식 패키지/버전/명령/필요권한을 먼저 보여주고 사용자가 실행할 설치 작업을 명시적으로 선택한다. 등록/인증 변경은 프로젝트 설정만 변경하며 다른 전역 MCP 설정을 자동 덮어쓰지 않는다. Docker bridge는 등록 image/version/profile·worktree mount만 사용한다.
- 자동 선택은 해당 단계에 필요한 capability와 프로젝트가 활성화한 서비스만 대상으로 한다. 유사한 서비스는 우선 제공자 설정에 따라 하나를 선택하고 다른 결과와 임의 혼합하지 않는다. `모든 도구`를 매 phase prompt에 넣지 않는다.
- 원격 응답에 들어 있는 링크/지시/도구 힌트는 untrusted data. tools/list의 readOnlyHint만으로 read 권한을 인정하지 않는다. provider별 검증된 tool→capability map, 입력 JSON schema, registered scope, node grant를 모두 확인한다. 새로운/알 수 없는 도구는 기본 deny.
- discovery/execute 등 범용 wrapper가 있는 제공자는 내부 실제 메서드·인수를 검사한다. 외부 서버가 discovery 결과로 노출한 쓰기 메서드를 읽기 grant로 호출하지 못한다.
- 호출의 provider/connection/tool/args digest/resource scope/snapshot/node/generation/result artifact digest/status/latency/usage를 기록한다. 인증헤더·토큰·DSN·환경변수 값·원본 개인정보를 이벤트/내보내기로 내보내지 않는다.
- 도구 결과는 기존 artifacts 경로로 digest를 기록하고 phase context에서 출처·조회시각·미검증/부분자료 사유를 포함한다. MCP 결과 안의 Harness Control은 phase 제어 블록으로 파싱하지 않는다.

## 4. Workflow 도구 노드와 역할 사용

- `kind: tool` 노드 추가: 등록 connection ID, operation ID, 승인된 입력, capability, resource scope, 필수/선택, timeout/usage, 결과 전달 대상. 스키마에서 JSON/타입과 node 참조를 검증한다.
- 입력값은 저장된 설정 또는 검증된 task 메타데이터 참조만 사용한다. 임의 template eval/쉘/JS/SQL 자동 실행 표현식을 받지 않는다. SQL/query는 provider read-only 역할과 허용 schema/row/time 정책을 검증한 등록 operation에서만 수행한다.
- 역할 노드에서 `필요 도구`를 선택하면 준비 단계에서 호스트가 허용된 자료를 수집해 주입한다. 추가 검색은 노드의 등록 operation에 대한 구조화 요청을 호스트가 검증하여 처리한다. 회수/시간/자료 크기 예산을 적용하고 API 모델에도 동일 bundle을 제공한다. ambient MCP 개방과 모든 tool의 자동 노출은 하지 않는다.
- 기본 budgets: node당 최대 tool call 10회, 호출 timeout30초, 단일 텍스트 결과2MB, 명세 context 발췌20KB. 이미지/trace cache는 따로20MB 제한. 프로젝트 settings/snapshot에서 더 작은 예산 설정 가능. 브라우저검증/scan은 등록 operation별 timeout을 최대900초까지 명시적으로 설정한다.
- retry는 idempotent 읽기만 최대1회; 401/403은 반복하지 않는다. 429 Retry-After와 남은 실행 예산을 확인해 다음 재개 시각을 기록한다. 사용자승인/외부쓰기/배포는 자동 재시도하지 않고 request ID와 provider 결과로 중복 여부 확인.
- 선택 도구 failure는 fallback/Not Tested, 필수 검증 도구 failure는 미충족 gate로 기록한다. Stitch/Figma 원본 미동기화만으로 차단하지 않는 상위 정책은 유지한다.
- DB 도구는 metadata 우선, DB read-only 계정/서버 모드 강제. 임의 migration/DDL/DML/관리자 권한을 기본 활성화하지 않는다. DB 수정은 DBA 결과·등록된 개발환경·기존 migration 검증 흐름에서 수행한다.
- Browser 도구는 분리된 테스트 프로필·등록된 local/dev origin에서만 사용한다. 개발 테스트 interaction은 browser.test capability로 구분한다. 일반 읽기 grant에 script evaluation/file upload/download/arbitrary code tool을 포함하지 않는다.
- Sentry event/로그는 기본 개인정보/credential redaction 후 모델 context로 전달한다. Snyk는 등록된 org/worktree의 검사만 허용하며 코드 전송 여부·빌드도구 호출을 설정 미리보기에 표시한다.
- 문서/Issue/PR 게시·배포 같은 외부 쓰기는 별도의 named operation과 승인된 scope/payload digest를 요구한다. 로컬 초안→검토→승인→전송 구조이며 전체 도구 추가 요청을 메시지/문서 자동 발송 동의로 쓰지 않는다. 제공자가 쓰기 tool을 지원해도 이번 기본 프리셋은 read/inspect/test/scan만 사용한다.
- 기존 publishing/notification adapter는 유지하며 GitHub PR/머지와 알림을 새 MCP로 이중 발송하지 않는다. Stitch/Figma 원본 쓰기 capability는 카탈로그가 지원하지 않는다.

## 5. 사용자 화면·CLI·재사용

- `/integrations`: 용도별 MCP 카드·검색·내장/사용자 등록. 상태 배지, 공식문서, `연결 설정`, `설치 준비`, `인증`, `실제 읽기 검사`, `비활성화`.
- 설정 마법사: 도구 선택→연결 방식→프로젝트/저장소/space/DB/URL scope→private credential 또는 OAuth→도구 목록 검사→허용 기능 검토→저장→샘플 읽기. 인증만 성공하고 실제 자료 접근이 실패한 상태는 partial로 표시한다.
- Workflow palette에 도구 노드, role inspector에 단계별 필요한 도구, 실행 화면에 실제 선택/호출/근거/오류/예산. 설치와 계정 미준비 시 필요한 항목을 한 번에 안내한다.
- 공통 CLI `devroster integration catalog/list/show/configure/check/doctor/export/import/disable`; provider-specific `devroster figma/stitch`는 공통서비스 facade로 유지. OAuth terminal flow와 web flow는 같은 연결을 사용한다.
- `/api/integrations/catalog/list/detail/prepare/configure/auth/check/disable/export/import`는 기존 loopback Host/Origin/CSRF·크기제한 적용. 설치/조회/변경은 POST. 인증 callback은 state·PKCE·세션을 검증하고 token을 응답 본문/URL/log에 넣지 않는다.
- custom MCP: 고급 설정으로 공식 문서 URL·remote HTTPS endpoint 또는 로컬 executable·인증방식·namespace를 등록할 수 있다. 입력 endpoint의 요청 대상/DNS/private-address/redirect를 검사하고 loopback은 명시적 local 등록에만 허용한다. capability mapping 검토 전 도구는 실행할 수 없다.
- export/import는 catalog ID·설정·scope 이름·버전만; secret·OAuth store·개인 원본 cache·외부 절대경로 제외. 다른 프로젝트에서는 리소스 scope/credential을 다시 연결한다. inactive server 등록 자체는 기본 package dependency를 추가하지 않는다.

## 6. 도입/검증 완료 기준

1. 20개 MCP server + 1개 Gateway 기본 catalog와 제공자별 read operation/capability map을 패키지에 포함한다. catalog-only 항목을 ready로 표시하지 않는다.
2. HTTP(JSON/SSE/pagination/initialize/session/auth)·stdio(ndjson/kill/env isolation) 공통 adapter fixture 검증, 모든 provider의 최소 read response/error fixture 검증.
3. 설치 없이 catalog/web/CLI가 동작, 미설치/미인증/미지원client·잘못된scope별 명시적 준비상태. 연결된 서버만 실제 읽기 smoke, 계정 없는 제공자는 Not Tested.
4. role/workflow 선택→snapshot grant→host call→출처자료→artifact→재개·late result fencing. duplicate write·scope탈출·임의method·외부HTMLcontrol·secrets 노출·SQL쓰기·browserscript·oauthcallback위조 거부.
5. 코드없이 웹/CLI 등록·설정·확인·필요도구자동선택이 동일, 2개 프로젝트 export/import·scope 재매핑, 기본 wheel dependency0 유지.

기존 파일/Git/검증 명령/프로젝트 지식·체크포인트는 호스트의 기존 기능을 사용한다. 같은 기능의 Filesystem/Git/Memory/Sequential Thinking MCP를 필수로 중복 설치하지 않는다. 필요한 사용자 서버는 custom 등록으로 연결할 수 있다. Semgrep/ZAP 등 기존 CLI 검증기를 MCP라고 부르지 않는다.

## 7. 토큰 절약과 Ponytail 연동

- 사용자 “포니테일”은 [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)로 해석한다. 다른 도구를 뜻하면 재확인한다. Ponytail은 기존 코드/표준 라이브러리/플랫폼 기능을 우선하는 개발 지침이다. 현재 공식 README/INSTALL은 스킬·플러그인 설치를 안내한다. 과거 MCP README 경로는 404이고 현재 MCP 설치 경로를 확인하지 못했으므로 **검증된 MCP 서버 수에 포함하지 않는다**. 기존 AGENTS.md를 덮어쓰지 않고 선택형 스킬 연동으로 제공한다. 현재 미설치/미적용.
- Ponytail은 Developer/Reviewer에서 명시적으로 선택, 기본 lite; full/ultra는 사용자가 선택한다. 모드/지침 버전을 snapshot에 기록하며 기존 보안·접근성·오류 처리·필수 검증 기준을 생략하지 않는다. 자동 단순화나 코드 삭제를 도구 등록만으로 실행하지 않는다.
- Serena는 프로젝트 root/frontend/backend scope 내 symbol/read operation을 사용한다. 서버는 프로젝트별로 분리하고 지원 언어/인덱스 준비를 표시한다. 조회 실패·누락 시 기존 rg/범위 읽기로 복귀한다. 수정은 기존 Developer 경로를 유지한다.
- Context Mode는 호스트가 이미 수집한 허용 자료를 프로젝트별 로컬 색인에 넣고 필요한 발췌를 반환한다. 외부 자료 안 지시는 실행하지 않는다. 임의 execute/execute_file/batch_execute, upgrade/purge, 전역 hook 자동 설치는 기본 grant에 포함하지 않는다. 실행 기능이 필요한 작업은 기존 검증된 실행 경로에서 수행한다. 별도 프로세스를 OS 보안 sandbox로 간주하지 않는다. CLI별 hook 지원을 제품 기능으로 보장하지 않는다.
- 웹/CLI 프로젝트 설정에 토큰 절약 off/balanced/aggressive를 추가한다. 권장 balanced는 필요한 도구 schema만 전달·문서 중복 제거·원본 artifact 보존/발췌·digest 캐시·symbol 조회를 사용한다. aggressive도 오류 코드/검증 실패/보안 및 DB·FMEA 필수 제약/출처를 제거하지 않는다. 자료가 부족하면 원문 범위를 다시 읽는다.
- summary/발췌에는 artifact ID/digest/조회범위/원본 크기/전달 크기/생략 여부/원문 재조회 경로를 기록한다. 프로젝트별 캐시를 분리하고 secret을 색인 전 제거한다. 오래된 digest는 무효화한다. 설치 없이 호스트의 발췌·예산 정책과 기존 checkpoint가 동작한다.
- 실행 화면에서 도구 설명/자료 전달량·모델 usage를 따로 표시한다. tokenizer 없는 모델은 추정치로 표시한다. bytes 감소율을 실제 청구 token 감소율로 표시하지 않는다. 같은 task/model/effort/input에서 off와 balanced를 비교하고 입력·출력·가능한 reasoning/cache usage·재조회·정확성·완료율을 함께 측정한다. 공급자 광고 절감률은 이 프로젝트의 보장치로 쓰지 않는다.
- 완료 기준: 두 MCP의 read fixture/준비상태·누락 시 fallback, Ponytail 스킬 상태/정책 우선순위, 원문 복원/출처·캐시 분리·비밀정보 필터, 절약 설정 export/import, 지표 실제/추정 구분, 동일 작업 품질 비교. 실설치/실호출/A-B 벤치마크는 현재 Not Tested.

## 8. Graphify 선택 연동

- 웹 조회는 `/source` 화면으로 구체화한다. 사용자가 파일·폴더 트리 + Graphify 그래프를 선택했으며 `/source`에서 코드/원문 위치와 관계 graph를 상호 탐색한다. 실제 UI/API 구현 완료를 의미하지 않는다.
- 사용자 추가 요청 반영. Graphify는 프로젝트 관계 탐색, Serena는 상세 symbol 조회, Context Mode는 자료 발췌/검색에 사용한다. 소규모 변경은 기존 rg/범위 읽기를 유지하고 구조 분석·리팩토링·변경 영향 확인에 graph.read를 추천한다.
- 공식 PyPI 패키지는 `graphifyy`(double-y), MCP extra는 `mcp`. 설치 마법사에서 공식 source/고정 version/격리 environment를 표시한다. `graphify install`은 스킬/AGENTS/hooks를 변경할 수 있으므로 MCP 설정과 구분하며 자동 실행하지 않는다. 전역 설정·Git hook·공유 HTTP·Neo4j는 기본 활성화하지 않는다.
- 생성과 조회를 분리한다. 사용자가 선택한 root/frontend/backend 경로에 `graphify extract <scope> --code-only`를 실행하는 준비 작업을 등록한다. 기본 코드 분석은 로컬 AST이며 별도 LLM 호출을 하지 않는다. 문서/PDF 의미 분석은 별도 선택 작업으로 앱의 모델/effort·backend·전송 자료 범위·예산을 고정한다. 다른 환경변수 API key를 보고 provider를 자동 변경하지 않는다. [공식 설치·분석 안내](https://github.com/Graphify-Labs/graphify)
- graph artifact는 project/worktree/scope/tool-version/source-manifest-digest/생성시각/graph-digest와 함께 저장한다. source manifest는 uncommitted 내용·추가/삭제 파일까지 포함한다. 변경 시 stale로 표시하고 명시적 갱신 후 새 generation을 발행한다. 빌드 결과는 원자적으로 교체하며 진행 중 snapshot은 이전 digest에 고정한다. graph.json이 없거나 손상되면 미준비/오류를 표시하고 원문 조회로 복귀한다.
- 조회 allowlist는 실제 tools/list schema를 확인한 query_graph/get_node/get_neighbors/shortest_path만 사용한다. project_path 등 리소스 인수가 있으면 등록 scope와 동일한 graph로 강제한다. 다른 프로젝트 graph·절대 경로·상위경로·symlink 탈출과 전역 graph 등록은 거부한다. PR/triage 등 별도 기능은 기본 grant에서 제외한다. 결과 크기/노드·edge 수/탐색 깊이를 operation schema와 host 예산으로 제한한다.
- EXTRACTED/INFERRED 관계를 사용자에게 각각 `원문 근거`/`추론된 연결`로 표시하고 source 위치·조회범위·누락 이유를 보존한다. graph 관계는 실제 런타임 호출이나 검증 완료의 증거로 간주하지 않는다. 명세↔코드 연결은 후보로만 제시하고 사용자가 확인한다. FMEA의 Function/Failure Chain·평가·필수 gate를 그래프 추론으로 확정하지 않는다.
- `/integrations` Graphify 카드: 설치/graph 준비/최신성·scope·원문 출처·오류·갱신 동작. `자료 그래프 준비`와 `읽기 연결 검사`를 구분한다. 역할 inspector는 구조/변경 영향 질문에 필요한 부분 graph만 전달하고 전체 graph/report를 매 phase에 주입하지 않는다. 제공자 graph.html은 자동 실행하지 않고 기존 UI에서 검증된 JSON의 텍스트/노드·edge를 표현한다.
- 다른 프로젝트로 export/import할 때 graph 원본·secret·절대경로를 제외하고 새 scope에서 다시 생성한다. 토큰 절약 검증은 최초 생성/갱신·문서 LLM·조회·원문 재조회 비용을 모두 포함한다. 완료 기준은 code-only 준비/4 read fixture·stale/손상 fallback·scope 격리·근거/추론 구분·재매핑. 현재 실설치/빌드/호출/절감률 측정은 Not Tested.

## 9. 구현 상태 (2026-10-07)

- 카탈로그21개·웹/CLI 연결·공통 transport·read scope/schema·PKCE·secret-free import/export 구현. 실제 연결은 선택형이며 자동 설치 없음.
- Browser MCP는 격리/현재origin 보장 전까지 실제 read 차단, 도구 목록 진단만 지원. OAuth는 registered client ID·만료 후 재인증.
- Graphify code-only 빌드·취소·cache·stale 구현, 외부 실설치/빌드 Not Tested. Ponytail 외부 스킬 대신 내장 호환 지침 off/lite/full/ultra 선택. 컨텍스트 정책은 프로젝트 설정에 저장.
- 사용 절차: [웹 사용 가이드](web-user-guide.md).
