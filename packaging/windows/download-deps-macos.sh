#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY_VERSION="${PY_VERSION:-311}"
PLATFORM="${PLATFORM:-win_amd64}"
WHEEL_DIR="$ROOT/dep/windows/cp${PY_VERSION}-${PLATFORM}"

rm -rf "$WHEEL_DIR"
mkdir -p "$WHEEL_DIR"

python3 -m pip download \
  --dest "$WHEEL_DIR" \
  --platform "$PLATFORM" \
  --python-version "$PY_VERSION" \
  --implementation cp \
  --abi "cp${PY_VERSION}" \
  --only-binary=:all: \
  -r "$ROOT/requirements.txt" \
  -r "$ROOT/requirements-build.txt"

echo
echo "Windows dependency wheelhouse created:"
echo "  $WHEEL_DIR"
echo
echo "Copy the whole dep/ directory to the Windows machine before running:"
echo "  packaging\\windows\\build.cmd"
