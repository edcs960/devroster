#!/bin/bash
# Build DevRoster.app on macOS and install it into Applications.
#
#   1. Install Python 3.11 or later (python.org installer, or: brew install python@3.12).
#   2. Double-click scripts/build_macos.command in Finder,
#      or run it from a terminal:  scripts/build_macos.command
#
# Result: dist-app/DevRoster.app, copied to /Applications if you answer yes.
# An app built on this Mac opens without the "unidentified developer" warning.
set -u
cd "$(dirname "$0")/.." || exit 1

fail() {
  echo
  echo "빌드에 실패했습니다. 위의 오류 메시지를 확인해주세요."
  read -r -p "Enter를 누르면 창을 닫습니다." _
  exit 1
}

PY=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 이상이 필요합니다. python.org에서 설치하거나 'brew install python@3.12'를 실행한 뒤 다시 실행해주세요."
  fail
fi

if [ ! -d .build-venv ]; then
  echo "[1/3] Python 빌드 환경을 만듭니다... ($PY)"
  "$PY" -m venv .build-venv || fail
fi
echo "[2/3] DevRoster와 PyInstaller를 설치합니다..."
.build-venv/bin/python -m pip install --upgrade pip || fail
.build-venv/bin/python -m pip install -e ".[app]" || fail

echo "[3/3] DevRoster.app을 만들고 실행을 확인합니다..."
.build-venv/bin/python scripts/build_app.py --smoke || fail

APP="dist-app/DevRoster.app"
echo
echo "완료: $APP"
read -r -p "Applications 폴더에 설치할까요? 이미 있으면 새 버전으로 바꿉니다. [Y/n] " answer
case "${answer:-Y}" in
  [Nn]*)
    open dist-app
    ;;
  *)
    if pgrep -f "/Applications/DevRoster.app/Contents/MacOS/" >/dev/null 2>&1; then
      echo "DevRoster가 실행 중입니다. 화면 왼쪽 메뉴의 '앱 종료'로 끈 뒤 다시 실행해주세요."
      fail
    fi
    rm -rf /Applications/DevRoster.app && ditto "$APP" /Applications/DevRoster.app || fail
    echo "설치했습니다: /Applications/DevRoster.app (Launchpad나 Spotlight에서 DevRoster로 실행)"
    ;;
esac
read -r -p "Enter를 누르면 창을 닫습니다." _
