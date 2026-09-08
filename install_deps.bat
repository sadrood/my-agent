@echo off
rem ============================================================
rem  my_agent one-time bootstrap: install all deps from the
rem  manifest (requirements.txt + desktop/package-lock.json).
rem  Shared by CLI and Desktop launchers. Idempotent.
rem  ASCII-only + CRLF on purpose: cmd.exe parses bytes in the
rem  OEM codepage (GBK on Chinese Windows) - non-ASCII or LF
rem  line endings corrupt parsing.
rem ============================================================
setlocal
cd /d "%~dp0"

echo [1/5] Checking Python (need 3.11+)...
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [ERROR] Python not found. Install Python 3.11+ from python.org
  echo         and tick "Add to PATH" during install.
  pause
  exit /b 1
)
%PY% --version

echo [2/5] Creating virtualenv .venv ...
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create venv
    pause
    exit /b 1
  )
)
set "VPY=%~dp0.venv\Scripts\python.exe"

echo [3/5] Installing Python deps from requirements.txt ...
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed - check your network.
  pause
  exit /b 1
)
echo      Python deps installed.

echo [4/5] Installing Playwright chromium (for CLI browser tool) ...
"%VPY%" -m playwright install chromium >nul 2>&1
if errorlevel 1 echo      NOTE: chromium download failed. Retry later with: .venv\Scripts\python -m playwright install chromium

echo [5/5] Desktop Node deps (npm ci). CLI keeps working without Node.
where node >nul 2>&1
if errorlevel 1 (
  echo      Node.js not found. Desktop needs Node 18+. CLI is ready now.
) else (
  pushd "%~dp0desktop"
  if not exist "node_modules\electron\dist\electron.exe" (
    if exist "package-lock.json" (
      call npm ci --no-audit --no-fund
      if errorlevel 1 (
        echo [ERROR] npm ci failed
        popd
        pause
        exit /b 1
      )
    ) else (
      call npm install --no-audit --no-fund
      if errorlevel 1 (
        echo [ERROR] npm install failed
        popd
        pause
        exit /b 1
      )
    )
  )
  popd
  echo      Node deps ready.
)

if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo [NOTE] .env created from the template. Open it and fill in
  echo         your own API keys before first real task.
)

echo.
echo Bootstrap done! Now use:  Start-CLI.bat  or  Start-Desktop.bat
echo Re-run this script anytime to fill in missing deps.
if /i not "%1"=="silent" pause
