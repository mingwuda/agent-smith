#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY_VERSION="${PY_VERSION:-311}"
PLATFORM="${PLATFORM:-win_amd64}"
WHEEL_DIR="$ROOT/dep/windows/cp${PY_VERSION}-${PLATFORM}"

rm -rf "$WHEEL_DIR"
mkdir -p "$WHEEL_DIR"

python3 -m pip download \
  --no-cache-dir \
  --dest "$WHEEL_DIR" \
  --platform "$PLATFORM" \
  --python-version "$PY_VERSION" \
  --implementation cp \
  --abi "cp${PY_VERSION}" \
  --only-binary=:all: \
  -r "$ROOT/requirements.txt" \
  -r "$ROOT/requirements-build.txt"

python3 - "$WHEEL_DIR" <<'PY'
import sys
import zipfile
from pathlib import Path

wheel_dir = Path(sys.argv[1])
bad = []
for wheel in sorted(wheel_dir.glob("*.whl")):
    try:
        with zipfile.ZipFile(wheel) as zf:
            corrupt = zf.testzip()
        if corrupt:
            bad.append(f"{wheel.name}: corrupt member {corrupt}")
    except zipfile.BadZipFile:
        bad.append(f"{wheel.name}: not a valid wheel zip")

if bad:
    print("Invalid wheels downloaded:")
    for item in bad:
        print(f"  {item}")
    raise SystemExit(1)

print(f"Validated {len(list(wheel_dir.glob('*.whl')))} wheels.")
PY

echo
echo "Windows dependency wheelhouse created:"
echo "  $WHEEL_DIR"
echo
echo "Copy the whole dep/ directory to the Windows machine before running:"
echo "  packaging\\windows\\build.cmd"
