@echo off
setlocal

rem UTF-8 console so Chinese/emoji log output does not raise GBK encode errors.
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"
set AGENT_HOST=127.0.0.1
set AGENT_PORT=8899
set AGENT_OPEN_BROWSER=1

if not exist "%~dp0DesktopAgent.exe" (
  echo Error: DesktopAgent.exe not found in this folder.
  echo Please run the build script (packaging\windows\build.cmd) first,
  echo or make sure you extracted the full package.
  pause
  exit /b 1
)

echo Starting Desktop Agent...
echo Browser will open at http://%AGENT_HOST%:%AGENT_PORT%/
echo.

DesktopAgent.exe

echo.
echo Desktop Agent has stopped.
pause
