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
set "WHEEL_TAG_FILE=%ROOT%\.venv-windows-build\wheel-tag.txt"

set "PIP_NO_DEPS="
set "PIP_CONFIG_FILE="
set "PIP_ONLY_BINARY="

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

"%PYTHON%" -m pip --isolated install --upgrade pip
if errorlevel 1 exit /b 1

"%PYTHON%" -c "import platform,sys; arch='win_amd64' if platform.machine().lower() in ('amd64','x86_64') else 'win32'; print(f'cp{sys.version_info[0]}{sys.version_info[1]}-{arch}')" > "%WHEEL_TAG_FILE%"
set /p WHEEL_TAG=<"%WHEEL_TAG_FILE%"
if "%WHEEL_TAG%"=="" (
  echo Error: failed to detect Python wheel tag.
  exit /b 1
)
set "WHEEL_DIR=%ROOT%\dep\windows\%WHEEL_TAG%"

if exist "%WHEEL_DIR%" (
  echo Installing dependencies from local wheelhouse:
  echo   %WHEEL_DIR%
  "%PYTHON%" -m pip --isolated install --upgrade --force-reinstall --no-index --find-links "%WHEEL_DIR%" -r "%ROOT%\requirements.txt" -r "%ROOT%\requirements-build.txt"
) else (
  echo Local wheelhouse not found, installing dependencies from package index.
  echo Expected local wheelhouse:
  echo   %WHEEL_DIR%
  "%PYTHON%" -m pip --isolated install --upgrade --force-reinstall -r "%ROOT%\requirements.txt" -r "%ROOT%\requirements-build.txt"
)
if errorlevel 1 exit /b 1

echo Installed packages:
"%PYTHON%" -m pip --isolated list
echo.

"%PYTHON%" -c "import altgraph; import packaging; import pefile; import PyInstaller; import win32ctypes.pywin32" >nul 2>nul
if errorlevel 1 (
  echo Error: PyInstaller Windows helper dependencies are missing.
  echo.
  echo If you are building offline, make sure these wheels exist:
  echo   %WHEEL_DIR%\altgraph-*.whl
  echo   %WHEEL_DIR%\packaging-*.whl
  echo   %WHEEL_DIR%\pefile-*.whl
  echo   %WHEEL_DIR%\pyinstaller-*.whl
  echo   %WHEEL_DIR%\pyinstaller_hooks_contrib-*.whl
  echo   %WHEEL_DIR%\pywin32_ctypes-*.whl
  echo.
  echo Recreate the macOS wheelhouse after updating requirements-build.txt:
  echo   PY_VERSION=311 PLATFORM=win_amd64 bash packaging/windows/download-deps-macos.sh
  echo.
  echo Then delete the old Windows build venv and run again:
  echo   rmdir /s /q "%VENV%"
  echo   packaging\windows\build.cmd
  exit /b 1
)

"%PYTHON%" -m pip --isolated check
if errorlevel 1 (
  echo Error: installed packages have dependency conflicts.
  "%PYTHON%" -m pip --isolated list
  exit /b 1
)

"%PYTHON%" -c "import fastapi; import starlette; import uvicorn; import pydantic; print('Runtime web dependency imports OK')"
if errorlevel 1 (
  echo Error: runtime web dependencies are missing from the build environment.
  echo.
  "%PYTHON%" -m pip --isolated list
  echo.
  echo If you are building offline, recreate the macOS wheelhouse and make sure these wheels exist:
  echo   %WHEEL_DIR%\fastapi-*.whl
  echo   %WHEEL_DIR%\starlette-*.whl
  echo   %WHEEL_DIR%\uvicorn-*.whl
  echo   %WHEEL_DIR%\pydantic-*.whl
  echo.
  echo Then delete the old Windows build venv and run again:
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
