@echo off
setlocal
cd /d "%~dp0"
echo Pulling latest code from GitHub...
git pull --ff-only
if errorlevel 1 (
  echo [ERROR] git pull failed - check network or local changes.
  pause
  exit /b 1
)
echo Filling any missing dependencies...
call install_deps.bat silent
echo.
echo Update done! Launchers in this folder:
for %%F in (install_deps.bat *.bat) do echo    %%F
echo Choose the CLI or Desktop launcher above.
pause
