@echo off
setlocal
cd /d "%~dp0"

rem Uses the project's own upgrader: it checks for a dirty worktree, detects
rem diverged history, and installs missing dependencies. A bare
rem "git pull --ff-only" just fails after a history rewrite and prints a
rem misleading "check network" message.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Updating (git pull + dependencies)...
"%PY%" main.py --upgrade
if errorlevel 1 (
  echo.
  echo [ERROR] Update did not finish. Follow the instructions above, then rerun this file.
  pause
  exit /b 1
)

echo.
echo Update done!
pause
