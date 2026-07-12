@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem ============================================================
rem  build-electron.cmd
rem  One-click build of the Electron installer (nsis .exe).
rem  Flow: build PyInstaller backend product -> electron-builder pack
rem  Output: dist\electron\DesktopAgent-Setup-<version>.exe
rem
rem  Backend build is AUTO-DETECTED by default:
rem    - if dist\windows\DesktopAgent-Windows\DesktopAgent.exe exists,
rem      the backend build is skipped automatically (fast repack);
rem    - otherwise the backend is built first.
rem
rem  Usage:
rem    packaging\windows\build-electron.cmd
rem        auto: skip backend build if product exists, else build it
rem    packaging\windows\build-electron.cmd --rebuild-backend
rem        force a fresh backend build even if a product exists
rem ============================================================

set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "ELECTRON_DIR=%ROOT%\electron"
set "PACKAGE_DIR=%ROOT%\dist\windows\DesktopAgent-Windows"
set "FORCE_BACKEND=0"

:parse_args
if "%~1"=="" goto after_args
if /I "%~1"=="--rebuild-backend" set "FORCE_BACKEND=1"
if /I "%~1"=="--force-backend" set "FORCE_BACKEND=1"
shift
goto parse_args
:after_args

cd /d "%ROOT%" || exit /b 1

rem ---- 1) Build PyInstaller backend product (auto-detect) ----
if "%FORCE_BACKEND%"=="1" goto do_build
if exist "%PACKAGE_DIR%\DesktopAgent.exe" goto auto_skip
goto do_build

:auto_skip
echo [1/3] Found existing backend product, skipping backend build.
echo       %PACKAGE_DIR%\DesktopAgent.exe
echo       ^(use --rebuild-backend to force a fresh backend build^)
goto backend_done

:do_build
echo [1/3] Building PyInstaller backend product...
call "%ROOT%\packaging\windows\build.cmd"
if errorlevel 1 (
  echo Error: backend build failed. See output above.
  exit /b 1
)
if not exist "%PACKAGE_DIR%\DesktopAgent.exe" (
  echo Error: backend build finished but DesktopAgent.exe is missing at:
  echo   %PACKAGE_DIR%\DesktopAgent.exe
  exit /b 1
)
goto backend_done

:backend_done

rem ---- 2) Locate node and check electron toolchain ----
echo [2/3] Checking Electron toolchain...
set "NODE_EXE="
where node >nul 2>nul && set "NODE_EXE=node"
if not defined NODE_EXE (
  for /f "delims=" %%N in ('dir /b /ad "%USERPROFILE%\.workbuddy\binaries\node\versions" 2^>nul') do set "NODE_EXE=%USERPROFILE%\.workbuddy\binaries\node\versions\%%N\node.exe"
)
if not defined NODE_EXE (
  echo Error: node was not found on PATH.
  echo Install Node.js 18+ or add it to PATH, then retry.
  exit /b 1
)

if not exist "%ELECTRON_DIR%\node_modules\electron-builder\cli.js" (
  echo Error: electron-builder is not installed.
  echo Run once in the electron/ directory:
  echo   cd electron ^&^& npm install
  exit /b 1
)

if not exist "%ELECTRON_DIR%\node_modules\electron\dist\electron.exe" (
  echo Error: electron runtime binary is missing.
  echo Fetch it via mirror in the electron/ directory:
  echo   set ELECTRON_MIRROR=https://registry.npmmirror.com/-/binary/electron/
  echo   node node_modules\electron\install.js
  exit /b 1
)

rem ---- 3) Pack with electron-builder ----
echo [3/3] Packaging with electron-builder...
cd /d "%ELECTRON_DIR%" || exit /b 1

rem Some sandboxes set ELECTRON_RUN_AS_NODE=1 globally, which breaks packaging. Clear it.
set "ELECTRON_RUN_AS_NODE="

rem Download tools via mirror (default npmmirror if not set; respect user override).
if not defined ELECTRON_MIRROR set "ELECTRON_MIRROR=https://registry.npmmirror.com/-/binary/electron/"
if not defined ELECTRON_BUILDER_BINARIES_MIRROR set "ELECTRON_BUILDER_BINARIES_MIRROR=https://registry.npmmirror.com/-/binary/electron-builder-binaries/"

"%NODE_EXE%" "%ELECTRON_DIR%\node_modules\electron-builder\cli.js" --win --x64
if errorlevel 1 (
  echo Error: electron-builder failed. See output above.
  exit /b 1
)

echo.
echo ============================================================
echo Electron installer created under:
echo   %ROOT%\dist\electron\
for %%F in ("%ROOT%\dist\electron\*.exe") do echo   %%~nxF  (%%~zF bytes)
echo ============================================================
echo Distribute the DesktopAgent-Setup-*.exe. It bundles the Electron
echo runtime + Python backend + Chromium. End users just double-click
echo to install and run - no browser, no port, no download needed.

endlocal
