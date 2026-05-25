$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Venv = Join-Path $Root ".venv-windows-build"
$Python = Join-Path $Venv "Scripts\python.exe"
$PyInstaller = Join-Path $Venv "Scripts\pyinstaller.exe"
$Spec = Join-Path $PSScriptRoot "DesktopAgent.spec"
$BuildRoot = Join-Path $Root "dist"
$PackageRoot = Join-Path $BuildRoot "windows"
$PackageDir = Join-Path $PackageRoot "DesktopAgent-Windows"
$ZipPath = Join-Path $PackageRoot "DesktopAgent-Windows.zip"

Set-Location $Root

if (-not (Test-Path $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv $Venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv $Venv
    } else {
        throw "Python 3 was not found. Install Python 3.10+ from https://www.python.org/downloads/windows/."
    }
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $Root "requirements.txt") -r (Join-Path $Root "requirements-build.txt")

& $PyInstaller --clean --noconfirm $Spec

if (Test-Path $PackageDir) {
    Remove-Item $PackageDir -Recurse -Force
}
New-Item -ItemType Directory -Path $PackageRoot -Force | Out-Null
Copy-Item (Join-Path $BuildRoot "DesktopAgent") $PackageDir -Recurse
Copy-Item (Join-Path $PSScriptRoot "Start Desktop Agent.bat") (Join-Path $PackageDir "Start Desktop Agent.bat")
Copy-Item (Join-Path $PSScriptRoot "README-Windows.txt") (Join-Path $PackageDir "README-Windows.txt")

if (Test-Path $ZipPath) {
    Remove-Item $ZipPath -Force
}
Compress-Archive -Path (Join-Path $PackageDir "*") -DestinationPath $ZipPath

Write-Host ""
Write-Host "Windows package created:"
Write-Host "  $PackageDir"
Write-Host "  $ZipPath"
