@echo off
setlocal
cd /d "%~dp0"

rem Prefer the prebuilt portable desktop build if present.
for /f "delims=" %%E in ('dir /b "desktop\release\win-unpacked\*.exe" 2^>nul ^| findstr /i "desktop"') do (
  echo Launching packaged desktop build...
  start "" "desktop\release\win-unpacked\%%E"
  goto :done
)

rem Otherwise: bootstrap python + node deps, then dev mode.
if not exist ".venv\Scripts\python.exe" (
  echo First run: bootstrapping dependencies...
  call install_deps.bat
  if errorlevel 1 exit /b 1
)
if not exist "desktop\node_modules\electron\dist\electron.exe" (
  echo Filling desktop Node deps...
  call install_deps.bat
  if errorlevel 1 exit /b 1
)
where node >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Node.js not found. Desktop needs Node 18+.
  echo         Install from https://nodejs.org then re-run this script.
  pause
  exit /b 1
)
echo Launching desktop in dev mode (Vite + Electron + built-in backend)...
cd /d "%~dp0desktop"
call npm run desktop:dev
goto :done

:done
