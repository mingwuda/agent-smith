#!/usr/bin/env bash
#
# Build the distributable Desktop Agent Electron app for macOS.
# Chains: PyInstaller backend (build.sh) -> electron-builder (dmg).
#
# MUST run on macOS. Produces: dist/electron/Desktop Agent-0.1.0-arm64.dmg
#
# Usage:
#   packaging/macos/build-electron-mac.sh                      # full build (backend + app, auto-skip if product exists)
#   packaging/macos/build-electron-mac.sh --rebuild-backend    # force rebuild backend even if product exists
#   packaging/macos/build-electron-mac.sh --x64                # build for Intel Macs
#   packaging/macos/build-electron-mac.sh --universal          # universal (arm64 + x64)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ELECTRON_DIR="$ROOT/electron"

FORCE_BACKEND=0
EB_ARCH="--arm64"
for arg in "$@"; do
  case "$arg" in
    --rebuild-backend) FORCE_BACKEND=1 ;;
    --x64) EB_ARCH="--x64" ;;
    --universal) EB_ARCH="--universal" ;;
  esac
done

# Locate node (managed first, then PATH)
if [ -x "$HOME/.workbuddy/binaries/node/versions/22.22.2/node" ]; then
  NODE="$HOME/.workbuddy/binaries/node/versions/22.22.2/node"
elif command -v node >/dev/null 2>&1; then
  NODE="node"
else
  echo "Error: Node.js not found. Install Node 18+ and retry."
  exit 1
fi

# ELECTRON_RUN_AS_NODE must NOT be set, or Electron degrades to plain Node
unset ELECTRON_RUN_AS_NODE 2>/dev/null || true

cd "$ELECTRON_DIR"

# Build backend unless product already exists (auto-detect)
if [ "$FORCE_BACKEND" -eq 1 ]; then
  echo "=== Building Python backend (PyInstaller, forced) ==="
  bash "$SCRIPT_DIR/build.sh"
elif [ -x "$ROOT/dist/macos/DesktopAgent-macOS/DesktopAgent" ]; then
  echo "=== Found existing backend product, skipping backend build ==="
  echo "    $ROOT/dist/macos/DesktopAgent-macOS/DesktopAgent"
  echo "    (use --rebuild-backend to force a fresh backend build)"
else
  echo "=== Building Python backend (PyInstaller) ==="
  bash "$SCRIPT_DIR/build.sh"
fi

# Verify electron-builder dependency
if [ ! -x "$ELECTRON_DIR/node_modules/.bin/electron-builder" ]; then
  echo "Error: electron-builder not installed."
  echo "Run: cd electron && ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm install"
  exit 1
fi

# Mirrors (optional, for faster downloads on restricted networks)
export ELECTRON_MIRROR="${ELECTRON_MIRROR:-https://registry.npmmirror.com/-/binary/electron/}"
export ELECTRON_BUILDER_BINARIES_MIRROR="${ELECTRON_BUILDER_BINARIES_MIRROR:-https://registry.npmmirror.com/-/binary/electron-builder-binaries/}"

echo "=== Running electron-builder (macOS, $EB_ARCH) ==="
"$NODE" node_modules/electron-builder/cli.js --mac "$EB_ARCH"

echo
echo "macOS app built. Find it in dist/electron/ (a .dmg installer)."
echo
echo "IMPORTANT - distribution:"
echo "  The .dmg is produced unsigned by default. macOS Gatekeeper will block it on other"
echo "  machines ('cannot be opened because the developer cannot be verified'). To distribute:"
echo "    1. Set a signing identity in electron/package.json ('mac.identity')."
echo "    2. Enable notarization (afterSign hook or 'mac.notarize' with an App Store Connect key)."
echo "    3. Re-run this script. See electron/package.json 'mac'/'dmg' config."
