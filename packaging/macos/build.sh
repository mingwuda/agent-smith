#!/usr/bin/env bash
#
# Build the Desktop Agent Python backend as a PyInstaller one-folder bundle for macOS.
# Produces: dist/macos/DesktopAgent-macOS/  (contains the "DesktopAgent" binary + _internal/ with Chromium)
#
# MUST run on macOS - PyInstaller cannot cross-compile. On Apple Silicon it builds arm64,
# on an Intel Mac it builds x64 automatically (controlled by the running Python/arch).
#
# Usage:
#   packaging/macos/build.sh            # full build (venv + deps + pyinstaller + browsers)
#   packaging/macos/build.sh --clean    # wipe the build venv first
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$ROOT/.venv-macos-build"
PYTHON="$VENV/bin/python"
PYINSTALLER="$VENV/bin/pyinstaller"
# The .spec is platform-agnostic: it has no BUNDLE step, so on macOS PyInstaller emits a
# plain one-folder dir (dist/DesktopAgent/) instead of a nested .app, which is exactly what
# the Electron shell expects (it spawns resources/agent/DesktopAgent).
SPEC="$ROOT/packaging/windows/DesktopAgent.spec"
BUILD_ROOT="$ROOT/dist"
PKG_DIR="$BUILD_ROOT/macos/DesktopAgent-macOS"

PIP_CACHE_DIR="$ROOT/.pip-cache"
mkdir -p "$PIP_CACHE_DIR"

# Optional PyPI mirror
PIP_INDEX_ARG=""
PIP_TRUST_ARG=""
if [ -n "${DESKTOP_AGENT_PIP_INDEX_URL:-}" ]; then
  PIP_INDEX_ARG="--index-url $DESKTOP_AGENT_PIP_INDEX_URL"
  if [ -n "${DESKTOP_AGENT_PIP_TRUSTED_HOST:-}" ]; then
    PIP_TRUST_ARG="--trusted-host $DESKTOP_AGENT_PIP_TRUSTED_HOST"
  fi
fi

cd "$ROOT"

if [ "${1:-}" == "--clean" ] && [ -d "$VENV" ]; then
  echo "Cleaning build venv..."
  rm -rf "$VENV"
fi

if [ ! -x "$PYTHON" ]; then
  python3 -m venv "$VENV"
fi

"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install $PIP_INDEX_ARG $PIP_TRUST_ARG --cache-dir "$PIP_CACHE_DIR" \
  -r "$ROOT/requirements.txt" -r "$ROOT/requirements-build.txt"

# Bundle Chromium so the packaged app is self-contained (no network on target machine).
# Prefer the local Playwright cache; only download if missing.
PLAYWRIGHT_BROWSERS_PATH="$ROOT/.playwright-browsers"
LOCAL_BROWSERS="$HOME/Library/Caches/ms-playwright"
if [ -d "$LOCAL_BROWSERS" ]; then
  echo "Copying Chromium from local Playwright cache - no download needed"
  "$PYTHON" - <<PY
import shutil
src = "$LOCAL_BROWSERS"
dst = "$PLAYWRIGHT_BROWSERS_PATH"
shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(src, dst)
PY
else
  echo "Local Playwright cache not found - downloading Chromium (needs network)"
  mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
  "$PYTHON" -m playwright install chromium
fi

"$PYINSTALLER" --clean --noconfirm "$SPEC"

SRC="$BUILD_ROOT/DesktopAgent"
if [ ! -x "$SRC/DesktopAgent" ]; then
  echo "Error: PyInstaller did not produce dist/DesktopAgent/DesktopAgent"
  echo "Check the build output above for missing modules or import errors."
  exit 1
fi

# Final package dir: copy bundle, then inject Chromium into _internal/ms-playwright.
# Same approach as Windows - browser binaries are NOT collected by PyInstaller (the EXE-as
# directory-node corruption); main.py points PLAYWRIGHT_BROWSERS_PATH at _internal/ms-playwright.
rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR"
cp -R "$SRC/." "$PKG_DIR/"

BROWSERS_SRC="$ROOT/.playwright-browsers"
BROWSERS_DST="$PKG_DIR/_internal/ms-playwright"
if [ -d "$BROWSERS_SRC" ]; then
  rm -rf "$BROWSERS_DST"
  cp -R "$BROWSERS_SRC" "$BROWSERS_DST"
  echo "Browsers bundled: $BROWSERS_DST"
else
  echo "Warning: .playwright-browsers not found - browser tool will not work in package."
fi

# Clean PyInstaller intermediate output
rm -rf "$SRC" "$ROOT/build/DesktopAgent"

echo
echo "macOS backend package created:"
echo "  $PKG_DIR"
echo "Next: run packaging/macos/build-electron-mac.sh to bundle it into the Electron app."
