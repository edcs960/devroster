---
name: screen-check
description: agent_dev/ui/assets 화면을 바꾼 뒤 실제 앱에서 4가지 폭·키보드·오류를 확인할 때
roles: [developer, qa, ux-ui]
---
# 화면 변경 확인

1. 격리된 데모 작업 공간으로 앱을 띄운다. 실제 사용자 데이터 폴더는 쓰지 않는다.
   `DEVROSTER_HOME=<임시 폴더> .venv/bin/devroster app --root <데모 프로젝트 복사본> --port 8766 --no-browser`
2. 바꾼 화면을 1440·1024·768·390px 폭으로 연다. 폭마다 확인한다.
   - `document.documentElement.scrollWidth - innerWidth`가 0이다(가로 넘침 없음).
   - 버튼·입력이 겹치거나 잘리지 않는다.
3. 키보드만으로 주요 동작을 끝낼 수 있다(Tab 순서, Enter/Space, Esc로 닫기, 닫은 뒤 포커스 복귀).
4. 브라우저 콘솔에 스크립트 오류와 CSP 위반이 없다.
5. 성공은 토스트, 입력 오류는 필드 아래, 막히는 오류는 해결 행동이 있는 배너로 나오는지 본다.
6. 관련 브라우저 테스트(`tests/*_browser.cjs`, `tests/*.js`)를 Playwright 환경변수를 지정해 실행한다.
7. 확인한 폭·결과·스크린샷 위치를 작업 결과에 적는다. 확인하지 못한 항목은 이유와 함께 적는다.
