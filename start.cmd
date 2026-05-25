@echo off
setlocal EnableExtensions

set "ROOT=%~dp0"
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "VENV=%ROOT%\.venv"
set "PYTHON=%VENV%\Scripts\python.exe"

cd /d "%ROOT%" || exit /b 1

if not exist "%PYTHON%" (
  echo Creating virtual environment...
  where py >nul 2>nul
  if %ERRORLEVEL%==0 (
    py -3 -m venv "%VENV%"
  ) else (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 (
      python -m venv "%VENV%"
    ) else (
      echo Error: Python 3 was not found.
      echo Install Python 3.10+ from https://www.python.org/downloads/windows/.
      pause
      exit /b 1
    )
  )
)

echo Installing dependencies...
if exist "%ROOT%\dep\windows" (
  "%PYTHON%" -c "import platform,sys; arch='win_amd64' if platform.machine().lower() in ('amd64','x86_64') else 'win32'; print(f'cp{sys.version_info[0]}{sys.version_info[1]}-{arch}')" > "%VENV%\wheel-tag.txt"
  set /p WHEEL_TAG=<"%VENV%\wheel-tag.txt"
  set "WHEEL_DIR=%ROOT%\dep\windows\%WHEEL_TAG%"
)

if defined WHEEL_DIR if exist "%WHEEL_DIR%" (
  echo Using local wheelhouse:
  echo   %WHEEL_DIR%
  "%PYTHON%" -m pip install --upgrade --force-reinstall --no-index --find-links "%WHEEL_DIR%" -r "%ROOT%\requirements.txt"
) else (
  "%PYTHON%" -m pip install --upgrade -r "%ROOT%\requirements.txt"
)
if errorlevel 1 (
  echo.
  echo Error: failed to install dependencies.
  pause
  exit /b 1
)

"%PYTHON%" -m pip check
if errorlevel 1 (
  echo.
  echo Error: installed dependencies are incomplete or conflicting.
  "%PYTHON%" -m pip list
  pause
  exit /b 1
)

set "AGENT_HOST=127.0.0.1"
set "AGENT_PORT=8899"
set "AGENT_OPEN_BROWSER=1"

echo.
echo Starting Desktop Agent...
echo   http://%AGENT_HOST%:%AGENT_PORT%/
echo.

cd /d "%ROOT%\agent_core" || exit /b 1
"%PYTHON%" main.py

echo.
echo Desktop Agent has stopped.
pause
