@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: bootstrapping dependencies...
  call install_deps.bat
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" main.py %*
