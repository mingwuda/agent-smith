@echo off
setlocal

cd /d "%~dp0"
set AGENT_HOST=127.0.0.1
set AGENT_PORT=8899
set AGENT_OPEN_BROWSER=1

echo Starting Desktop Agent...
echo Browser will open at http://%AGENT_HOST%:%AGENT_PORT%/
echo.

DesktopAgent.exe

echo.
echo Desktop Agent has stopped.
pause
