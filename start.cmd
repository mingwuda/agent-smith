@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT=%~dp0"
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV=%ROOT%\.venv"
set "PYTHON=%VENV%\Scripts\python.exe"

cd /d "%ROOT%" || exit /b 1

:: ── [1/3] 检查 / 创建虚拟环境 ──
if not exist "%PYTHON%" (
  echo [1/3] 正在创建虚拟环境...
  where py >nul 2>nul
  if !ERRORLEVEL! equ 0 (
    py -3 -m venv "%VENV%"
  ) else (
    where python >nul 2>nul
    if !ERRORLEVEL! equ 0 (
      python -m venv "%VENV%"
    ) else (
      echo 错误：未找到 Python 3。
      echo 请从 https://www.python.org/downloads/windows/ 安装 Python 3.10+。
      pause
      exit /b 1
    )
  )
  if !ERRORLEVEL! neq 0 (
    echo 错误：虚拟环境创建失败。
    pause
    exit /b 1
  )
  echo 正在升级 pip...
  "%PYTHON%" -m pip install --upgrade pip >nul 2>nul
) else (
  echo [1/3] 虚拟环境已存在。
)

:: ── [2/3] 检查 / 安装依赖 ──
echo [2/3] 检查依赖...

:: 用导入关键模块来判断依赖是否真的已安装（pip check 对空环境不报错）
"%PYTHON%" -c "import fastapi" >nul 2>nul
if !ERRORLEVEL! neq 0 (
  echo       正在安装依赖...
  "%PYTHON%" -m pip install --upgrade -r "%ROOT%\requirements.txt"
  if !ERRORLEVEL! neq 0 (
    echo.
    echo 错误：依赖安装失败。
    pause
    exit /b 1
  )
  :: 安装后做一次完整性校验
  "%PYTHON%" -m pip check >nul 2>nul
  if !ERRORLEVEL! neq 0 (
    echo.
    echo 警告：部分依赖可能不完整，将尝试继续启动。
    "%PYTHON%" -m pip list 2>nul
  )
) else (
  echo       依赖已就绪。
)

:: ── [3/3] 启动应用 ──
set "AGENT_HOST=127.0.0.1"
set "AGENT_PORT=8899"
set "AGENT_OPEN_BROWSER=1"

echo.
echo [3/3] 启动 Moss Agent...
echo   http://%AGENT_HOST%:%AGENT_PORT%/
echo.

cd /d "%ROOT%\agent_core" || exit /b 1
"%PYTHON%" main.py

echo.
echo Moss Agent 已停止。
pause
