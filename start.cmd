@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV=%ROOT%\.venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "WHEEL_TAG_FILE=%VENV%\wheel-tag.txt"
set "EMPTY_PIP_CONFIG=%VENV%\pip-empty.ini"
if not defined DESKTOP_AGENT_PIP_INDEX_URL set "DESKTOP_AGENT_PIP_INDEX_URL=http://maven.paic.com.cn:8445/repository/pypi/simple/"
if not defined DESKTOP_AGENT_PIP_TRUSTED_HOST set "DESKTOP_AGENT_PIP_TRUSTED_HOST=maven.paic.com.cn"
set "PIP_CONFIG_FILE=%EMPTY_PIP_CONFIG%"
set "PIP_NO_DEPS="
set "PIP_ONLY_BINARY="

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

type nul > "%EMPTY_PIP_CONFIG%"

echo Installing dependencies...
if exist "%ROOT%\dep\windows" (
  "%PYTHON%" -c "import platform,sys; arch='win_amd64' if platform.machine().lower() in ('amd64','x86_64') else 'win32'; print('cp{}{}-{}'.format(sys.version_info[0], sys.version_info[1], arch))" > "%WHEEL_TAG_FILE%"
  set /p WHEEL_TAG=<"%WHEEL_TAG_FILE%"
  if "!WHEEL_TAG!"=="" (
    echo Error: failed to detect Python wheel tag.
    pause
    exit /b 1
  )
  set "WHEEL_DIR=%ROOT%\dep\windows\!WHEEL_TAG!"
)

if defined WHEEL_DIR (
  if exist "%WHEEL_DIR%" (
    echo Using local wheelhouse:
    echo   %WHEEL_DIR%
    "%PYTHON%" -m pip --isolated install --upgrade --force-reinstall --no-index --find-links "%WHEEL_DIR%" -r "%ROOT%\requirements.txt"
  ) else (
    "%PYTHON%" -m pip --isolated install --index-url "%DESKTOP_AGENT_PIP_INDEX_URL%" --trusted-host "%DESKTOP_AGENT_PIP_TRUSTED_HOST%" --upgrade -r "%ROOT%\requirements.txt"
  )
) else (
  "%PYTHON%" -m pip --isolated install --index-url "%DESKTOP_AGENT_PIP_INDEX_URL%" --trusted-host "%DESKTOP_AGENT_PIP_TRUSTED_HOST%" --upgrade -r "%ROOT%\requirements.txt"
)
if errorlevel 1 (
  echo.
  echo Error: failed to install dependencies.
  pause
  exit /b 1
)

"%PYTHON%" -m pip --isolated check
if errorlevel 1 (
  echo.
  echo Error: installed dependencies are incomplete or conflicting.
  "%PYTHON%" -m pip --isolated list
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
