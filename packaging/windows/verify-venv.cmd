@echo off
setlocal EnableExtensions

set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "VENV=%ROOT%\.venv-windows-verify"
set "PYTHON=%VENV%\Scripts\python.exe"
set "EMPTY_PIP_CONFIG=%VENV%\pip-empty.ini"
if not defined DESKTOP_AGENT_PIP_INDEX_URL set "DESKTOP_AGENT_PIP_INDEX_URL=http://maven.paic.com.cn:8445/repository/pypi/simple/"
if not defined DESKTOP_AGENT_PIP_TRUSTED_HOST set "DESKTOP_AGENT_PIP_TRUSTED_HOST=maven.paic.com.cn"
set "PIP_CONFIG_FILE=%EMPTY_PIP_CONFIG%"
set "PIP_NO_DEPS="
set "PIP_ONLY_BINARY="

cd /d "%ROOT%" || exit /b 1

if exist "%VENV%" (
  echo Removing old verification virtual environment...
  rmdir /s /q "%VENV%"
)

echo Creating verification virtual environment...
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

(
  echo [install]
  echo no-dependencies = false
) > "%EMPTY_PIP_CONFIG%"

echo.
echo Python:
"%PYTHON%" --version

echo.
echo Pip configuration:
"%PYTHON%" -m pip config list
echo.
echo Using package index:
echo   %DESKTOP_AGENT_PIP_INDEX_URL%

echo.
echo Upgrading pip tooling...
"%PYTHON%" -m pip install --index-url "%DESKTOP_AGENT_PIP_INDEX_URL%" --trusted-host "%DESKTOP_AGENT_PIP_TRUSTED_HOST%" --upgrade pip setuptools
if errorlevel 1 exit /b 1

echo.
echo Installing runtime dependencies from package index...
echo This step uses pip --isolated with an explicit internal index, so global no-dependencies=yes is ignored.
"%PYTHON%" -m pip install --index-url "%DESKTOP_AGENT_PIP_INDEX_URL%" --trusted-host "%DESKTOP_AGENT_PIP_TRUSTED_HOST%" --upgrade --force-reinstall --no-cache-dir -r "%ROOT%\requirements.txt"
if errorlevel 1 (
  echo.
  echo Error: dependency installation failed.
  echo If the internal index does not mirror a required package, use the offline dep wheelhouse instead.
  pause
  exit /b 1
)

echo.
echo Checking dependency consistency...
"%PYTHON%" -c "import importlib.metadata as m; print('fastapi requires:', m.requires('fastapi'))"
"%PYTHON%" -m pip check
if errorlevel 1 (
  echo.
  echo Error: installed dependencies are incomplete or conflicting.
  "%PYTHON%" -m pip list
  pause
  exit /b 1
)

echo.
echo Verifying imports...
"%PYTHON%" -c "import fastapi, starlette, uvicorn, pydantic; import langchain, langchain_core, langchain_openai, langgraph; print('All required imports OK')"
if errorlevel 1 (
  echo.
  echo Error: import verification failed.
  "%PYTHON%" -m pip list
  pause
  exit /b 1
)

echo.
echo Dependency verification succeeded.
echo Virtual environment:
echo   %VENV%
echo.
echo To start the app with this environment, run:
echo   packaging\windows\verify-venv.cmd run

if /I "%~1"=="run" (
  set "AGENT_HOST=127.0.0.1"
  set "AGENT_PORT=8899"
  set "AGENT_OPEN_BROWSER=1"
  echo.
  echo Starting Desktop Agent...
  cd /d "%ROOT%\agent_core" || exit /b 1
  "%PYTHON%" main.py
)

endlocal
