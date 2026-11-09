@echo off
chcp 65001 >nul
rem Build DevRoster.exe on Windows (PyInstaller cannot build it from macOS or Linux).
rem
rem   1. Install Python 3.11 or later from python.org (check "Add python.exe to PATH").
rem   2. Open this folder in Explorer and double-click scripts\build_windows.bat,
rem      or run it from a terminal:  scripts\build_windows.bat
rem
rem Result: dist-app\DevRoster.exe  (one file; double-click to start DevRoster)
setlocal
cd /d "%~dp0\.."

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

if not exist .build-venv (
  echo [1/3] Python 빌드 환경을 만듭니다...
  %PY% -m venv .build-venv || goto :fail
)
echo [2/3] DevRoster와 PyInstaller를 설치합니다...
.build-venv\Scripts\python.exe -m pip install --upgrade pip || goto :fail
.build-venv\Scripts\python.exe -m pip install -e ".[app]" || goto :fail

echo [3/3] DevRoster.exe를 만들고 실행을 확인합니다...
.build-venv\Scripts\python.exe scripts\build_app.py --onefile --smoke || goto :fail

echo.
echo 완료: dist-app\DevRoster.exe
echo 처음 실행할 때 "Windows의 PC 보호" 창이 나오면 "추가 정보" - "실행"을 누르세요 (서명하지 않은 파일).
pause
exit /b 0

:fail
echo.
echo 빌드에 실패했습니다. 위의 오류 메시지를 확인해주세요.
pause
exit /b 1
