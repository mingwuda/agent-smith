@echo off
setlocal EnableExtensions

set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "VENV=%ROOT%\.venv-windows-verify"
set "PYTHON=%VENV%\Scripts\python.exe"

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

echo.
echo Python:
"%PYTHON%" --version

echo.
echo Pip configuration:
"%PYTHON%" -m pip config list

echo.
echo Upgrading pip tooling...
"%PYTHON%" -m pip install --upgrade pip setuptools
if errorlevel 1 exit /b 1

echo.
echo Installing runtime dependencies from package index...
"%PYTHON%" -m pip install --upgrade --force-reinstall --no-cache-dir -r "%ROOT%\requirements.txt"
if errorlevel 1 (
  echo.
  echo Error: dependency installation failed.
  pause
  exit /b 1
)

echo.
echo Checking dependency consistency...
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
