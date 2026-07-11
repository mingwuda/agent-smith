@echo off
setlocal EnableExtensions

set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "VENV=%ROOT%\.venv-windows-verify"
set "PYTHON=%VENV%\Scripts\python.exe"
set "EMPTY_PIP_CONFIG=%VENV%\pip-empty.ini"
set "PIP_CONFIG_FILE=%EMPTY_PIP_CONFIG%"
set "PIP_NO_DEPS="
set "PIP_ONLY_BINARY="

rem 本地 wheel 缓存：跨验证复用，避免重复下载
set "PIP_CACHE_DIR=%ROOT%\.pip-cache"
if not exist "%PIP_CACHE_DIR%" mkdir "%PIP_CACHE_DIR%"

rem 包索引：优先使用环境变量指定的镜像；未设置时回退官方 PyPI
set "PIP_INDEX_ARG="
set "PIP_TRUST_ARG="
if defined DESKTOP_AGENT_PIP_INDEX_URL (
  set "PIP_INDEX_ARG=--index-url %DESKTOP_AGENT_PIP_INDEX_URL%"
  if defined DESKTOP_AGENT_PIP_TRUSTED_HOST set "PIP_TRUST_ARG=--trusted-host %DESKTOP_AGENT_PIP_TRUSTED_HOST%"
)

cd /d "%ROOT%" || exit /b 1

rem 仅在传入 --clean 时重建验证环境；否则复用已有 venv + 本地 wheel 缓存
set "CLEAN_VENV=0"
if /I "%~1"=="--clean" set "CLEAN_VENV=1"
if "%CLEAN_VENV%"=="1" (
  if exist "%VENV%" (
    echo Removing old verification virtual environment...
    rmdir /s /q "%VENV%"
  )
)

if not exist "%PYTHON%" (
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
) else (
  echo Reusing existing verification virtual environment.
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
if defined DESKTOP_AGENT_PIP_INDEX_URL (
  echo   %DESKTOP_AGENT_PIP_INDEX_URL%
) else (
  echo   (official PyPI - set DESKTOP_AGENT_PIP_INDEX_URL to override)
)

echo.
rem 仅在显式要求时升级 pip/依赖；默认复用已装版本，避免重复下载
set "PIP_UPGRADE_ARG="
if "%DESKTOP_AGENT_PIP_UPGRADE%"=="1" set "PIP_UPGRADE_ARG=--upgrade"

rem 依赖指纹：requirements 内容未变化则跳过安装，直接复用已有 venv 与本地 wheel 缓存
set "REQ_SIG_FILE=%VENV%\.req-sig.txt"
set "CURRENT_SIG="
if exist "%PYTHON%" (
  for /f "usebackq delims=" %%s in (`"%PYTHON%" -c "import hashlib; h=hashlib.sha256(); h.update(open(r'%ROOT%\requirements.txt','rb').read()); print(h.hexdigest())"`) do set "CURRENT_SIG=%%s"
)
set "SKIP_INSTALL=0"
if exist "%REQ_SIG_FILE%" if defined CURRENT_SIG (
  for /f "usebackq delims=" %%p in ("%REQ_SIG_FILE%") do if "%%p"=="%CURRENT_SIG%" set "SKIP_INSTALL=1"
)

if "%SKIP_INSTALL%"=="1" (
  echo Dependencies unchanged - reusing cached venv and wheels.
) else (
  echo Installing runtime dependencies from package index...
  echo This step uses a temporary pip config with no-dependencies=false.
  "%PYTHON%" -m pip install %PIP_INDEX_ARG% %PIP_TRUST_ARG% %PIP_UPGRADE_ARG% --cache-dir "%PIP_CACHE_DIR%" -r "%ROOT%\requirements.txt"
  if errorlevel 1 (
    echo.
    echo Error: dependency installation failed.
    echo Check that the configured package index mirrors all required packages.
    pause
    exit /b 1
  )
  if defined CURRENT_SIG echo %CURRENT_SIG%> "%REQ_SIG_FILE%"
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
