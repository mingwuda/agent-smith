@echo off
setlocal EnableExtensions

set "ROOT=%~dp0..\.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "VENV=%ROOT%\.venv-windows-build"
set "PYTHON=%VENV%\Scripts\python.exe"
set "PYINSTALLER=%VENV%\Scripts\pyinstaller.exe"
set "SPEC=%ROOT%\packaging\windows\DesktopAgent.spec"
set "BUILD_ROOT=%ROOT%\dist"
set "PACKAGE_ROOT=%BUILD_ROOT%\windows"
set "PACKAGE_DIR=%PACKAGE_ROOT%\DesktopAgent-Windows"
set "ZIP_PATH=%PACKAGE_ROOT%\DesktopAgent-Windows.zip"
set "EMPTY_PIP_CONFIG=%ROOT%\.venv-windows-build\pip-empty.ini"

set "PIP_NO_DEPS="
set "PIP_CONFIG_FILE=%EMPTY_PIP_CONFIG%"
set "PIP_ONLY_BINARY="
if not defined DESKTOP_AGENT_PIP_INDEX_URL set "DESKTOP_AGENT_PIP_INDEX_URL=http://maven.paic.com.cn:8445/repository/pypi/simple/"
if not defined DESKTOP_AGENT_PIP_TRUSTED_HOST set "DESKTOP_AGENT_PIP_TRUSTED_HOST=maven.paic.com.cn"

cd /d "%ROOT%" || exit /b 1

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

"%PYTHON%" -m pip install --index-url "%DESKTOP_AGENT_PIP_INDEX_URL%" --trusted-host "%DESKTOP_AGENT_PIP_TRUSTED_HOST%" --upgrade pip
if errorlevel 1 exit /b 1

"%PYTHON%" -m pip install --index-url "%DESKTOP_AGENT_PIP_INDEX_URL%" --trusted-host "%DESKTOP_AGENT_PIP_TRUSTED_HOST%" --upgrade --force-reinstall -r "%ROOT%\requirements.txt" -r "%ROOT%\requirements-build.txt"
if errorlevel 1 exit /b 1

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

"%PYINSTALLER%" --clean --noconfirm "%SPEC%"
if errorlevel 1 exit /b 1

if exist "%PACKAGE_DIR%" rmdir /s /q "%PACKAGE_DIR%"
if not exist "%PACKAGE_ROOT%" mkdir "%PACKAGE_ROOT%"

xcopy "%BUILD_ROOT%\DesktopAgent" "%PACKAGE_DIR%\" /E /I /Y >nul
if errorlevel 1 exit /b 1

copy /Y "%ROOT%\packaging\windows\Start Desktop Agent.bat" "%PACKAGE_DIR%\Start Desktop Agent.bat" >nul
copy /Y "%ROOT%\packaging\windows\README-Windows.txt" "%PACKAGE_DIR%\README-Windows.txt" >nul

if exist "%ZIP_PATH%" del /f /q "%ZIP_PATH%"
where powershell >nul 2>nul
if %ERRORLEVEL%==0 (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '%PACKAGE_DIR%\*' -DestinationPath '%ZIP_PATH%' -Force" >nul 2>nul
)

echo.
echo Windows package created:
echo   %PACKAGE_DIR%
if exist "%ZIP_PATH%" (
  echo   %ZIP_PATH%
) else (
  echo Zip creation was skipped or blocked. Use the package directory above.
)

endlocal
