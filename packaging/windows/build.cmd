@echo off
setlocal EnableExtensions
chcp 65001 >nul

set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "VENV=%ROOT%\.venv-windows-build"
set "PYTHON=%VENV%\Scripts\python.exe"
set "PYINSTALLER=%VENV%\Scripts\pyinstaller.exe"
set "SPEC=%ROOT%\packaging\windows\DesktopAgent.spec"
set "BUILD_ROOT=%ROOT%\dist"
set "PACKAGE_ROOT=%BUILD_ROOT%\windows"
set "PACKAGE_DIR=%PACKAGE_ROOT%\DesktopAgent-Windows"
set "EMPTY_PIP_CONFIG=%ROOT%\.venv-windows-build\pip-empty.ini"

set "PIP_NO_DEPS="
set "PIP_CONFIG_FILE=%EMPTY_PIP_CONFIG%"
set "PIP_ONLY_BINARY="

rem 本地 wheel 缓存：跨打包复用，避免重复下载
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

rem 传入 --clean 时清空构建 venv，强制全新安装依赖
if /I "%~1"=="--clean" (
  if exist "%VENV%" (
    echo Cleaning build virtual environment...
    rmdir /s /q "%VENV%"
  )
)

if not exist "%PYTHON%" (
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
      exit /b 1
    )
  )
)

(
  echo [install]
  echo no-dependencies = false
) > "%EMPTY_PIP_CONFIG%"

rem 仅在显式要求时升级 pip/依赖；默认复用已装版本，避免重复下载
set "PIP_UPGRADE_ARG="
if "%DESKTOP_AGENT_PIP_UPGRADE%"=="1" set "PIP_UPGRADE_ARG=--upgrade"

rem 依赖指纹：requirements 内容未变化则跳过安装，直接复用已有 venv 与本地 wheel 缓存
set "REQ_SIG_FILE=%VENV%\.req-sig.txt"
set "CURRENT_SIG="
if exist "%PYTHON%" (
  for /f "usebackq delims=" %%s in (`"%PYTHON%" -c "import hashlib; h=hashlib.sha256(); [h.update(open(f,'rb').read()) for f in [r'%ROOT%\requirements.txt', r'%ROOT%\requirements-build.txt']]; print(h.hexdigest())"`) do set "CURRENT_SIG=%%s"
)
set "SKIP_INSTALL=0"
if exist "%REQ_SIG_FILE%" if defined CURRENT_SIG (
  for /f "usebackq delims=" %%p in ("%REQ_SIG_FILE%") do if "%%p"=="%CURRENT_SIG%" set "SKIP_INSTALL=1"
)

if "%SKIP_INSTALL%"=="1" (
  echo Dependencies unchanged since last build - reusing cached venv and wheels.
) else (
  "%PYTHON%" -m pip install %PIP_INDEX_ARG% %PIP_TRUST_ARG% %PIP_UPGRADE_ARG% --cache-dir "%PIP_CACHE_DIR%" pip
  if errorlevel 1 exit /b 1
  "%PYTHON%" -m pip install %PIP_INDEX_ARG% %PIP_TRUST_ARG% %PIP_UPGRADE_ARG% --cache-dir "%PIP_CACHE_DIR%" -r "%ROOT%\requirements.txt" -r "%ROOT%\requirements-build.txt"
  if errorlevel 1 exit /b 1
  if defined CURRENT_SIG echo %CURRENT_SIG%> "%REQ_SIG_FILE%"
)

echo Installed packages:
"%PYTHON%" -m pip list
echo.

"%PYTHON%" -c "import altgraph; import packaging; import pefile; import PyInstaller; import win32ctypes.pywin32" >nul 2>nul
if errorlevel 1 (
  echo Error: PyInstaller Windows helper dependencies are missing.
  echo.
  echo Delete the old Windows build venv and run again:
  echo   rmdir /s /q "%VENV%"
  echo   packaging\windows\build.cmd
  exit /b 1
)

"%PYTHON%" -m pip check
if errorlevel 1 (
  echo Error: installed packages have dependency conflicts.
  "%PYTHON%" -m pip list
  exit /b 1
)

"%PYTHON%" -c "import fastapi; import starlette; import uvicorn; import pydantic; print('Runtime web dependency imports OK')"
if errorlevel 1 (
  echo Error: runtime web dependencies are missing from the build environment.
  echo.
  "%PYTHON%" -m pip list
  echo.
  echo Delete the old Windows build venv and run again:
  echo   rmdir /s /q "%VENV%"
  echo   packaging\windows\build.cmd
  exit /b 1
)

rem Bundle Chromium so the packaged app is self-contained (no network needed on target machine).
rem Prefer copying from the local Playwright cache; only download if that cache is missing.
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\.playwright-browsers"
set "LOCAL_BROWSERS=%LOCALAPPDATA%\ms-playwright"
if exist "%LOCAL_BROWSERS%" (
  echo Copying Chromium from local Playwright cache - no download needed
  "%PYTHON%" -c "import shutil; shutil.rmtree(r'%PLAYWRIGHT_BROWSERS_PATH%', ignore_errors=True); shutil.copytree(r'%LOCAL_BROWSERS%', r'%PLAYWRIGHT_BROWSERS_PATH%')"
) else (
  echo Local Playwright cache not found - downloading Chromium - needs network
  if not exist "%PLAYWRIGHT_BROWSERS_PATH%" mkdir "%PLAYWRIGHT_BROWSERS_PATH%"
  "%PYTHON%" -m playwright install chromium
  if errorlevel 1 (
    echo Error: failed to install Chromium for bundling.
    echo Browser tool will not work in the packaged build.
    exit /b 1
  )
)

"%PYINSTALLER%" --clean --noconfirm "%SPEC%"
if errorlevel 1 exit /b 1

if not exist "%BUILD_ROOT%\DesktopAgent\DesktopAgent.exe" (
  echo Error: PyInstaller did not produce DesktopAgent.exe.
  echo Check the build output above for missing modules or import errors.
  exit /b 1
)

"%PYTHON%" "%ROOT%\packaging\windows\package_dist.py" "%BUILD_ROOT%\DesktopAgent" "%PACKAGE_DIR%" "%ROOT%"
if errorlevel 1 exit /b 1

rem 打包成功后删除 PyInstaller 中间产物，避免 dist 下残留第二个无用 exe
if exist "%BUILD_ROOT%\DesktopAgent" rmdir /s /q "%BUILD_ROOT%\DesktopAgent"
if exist "%ROOT%\build\DesktopAgent" rmdir /s /q "%ROOT%\build\DesktopAgent"

echo.
echo Backend bundle created (used as input for the Electron installer):
echo   %PACKAGE_DIR%
echo.
echo Next, build the desktop installer:
echo   packaging\windows\build-electron.cmd
echo It consumes the bundle above and produces dist\electron\DesktopAgent-Setup-0.1.1.exe
echo ^(the only Windows desktop deliverable - no extracted folder / Start script is distributed^).

endlocal
