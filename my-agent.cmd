@echo off
rem ============================================================
rem my_agent global launcher (ASCII only - cmd.exe safe)
rem Usage (from anywhere): my-agent "task" / my-agent --list-tools
rem Auto cd to project dir so .env / AGENTS.md / memory paths work
rem ============================================================
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py %*
) else (
    python main.py %*
)
exit /b %ERRORLEVEL%
