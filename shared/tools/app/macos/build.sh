#!/bin/bash
set -euo pipefail

# Build-only dependencies: macOS Command Line Tools, uv 0.11.28, network.
# The resulting InkSight.app contains its own Python runtime and wheels.
REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
APP_OUT="${1:-$REPO_ROOT/dist/macos}"
BUILD_TEMP="$(mktemp -d /tmp/inksight-macos-build.XXXXXX)"
APP_PATH="$APP_OUT/InkSight.app"
PY_DIR="$BUILD_TEMP/python/cpython-3.11.15-macos-aarch64-none"
mkdir -p "$APP_OUT" "$BUILD_TEMP/candidate"

if [ "$(uname -m)" != "arm64" ]; then
  echo "This build is only for macOS arm64" >&2
  exit 1
fi
if ! uv --version | grep -q '^uv 0\.11\.28 '; then
  echo "Build requires uv 0.11.28 for the pinned Python distribution catalog" >&2
  exit 1
fi

python3 "$REPO_ROOT/shared/tools/release/build_candidate.py" \
  --root "$REPO_ROOT" --destination "$BUILD_TEMP/candidate/InkSight-Source"
uv python install 3.11.15 --install-dir "$BUILD_TEMP/python" --no-bin
"$PY_DIR/bin/python3.11" -m pip install --quiet --require-hashes \
  --target "$BUILD_TEMP/site-packages" \
  -r "$REPO_ROOT/requirements-py311-macos-arm64.lock"

if [ -e "$APP_PATH" ]; then
  echo "Output exists; move it aside before rebuilding: $APP_PATH" >&2
  exit 1
fi
mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"
ditto "$PY_DIR" "$APP_PATH/Contents/Resources/Python"
ditto "$BUILD_TEMP/site-packages" \
  "$APP_PATH/Contents/Resources/Python/lib/python3.11/site-packages"
PYTHONDONTWRITEBYTECODE=1 "$APP_PATH/Contents/Resources/Python/bin/python3.11" \
  "$REPO_ROOT/shared/tools/app/macos/app_components.py" \
  --output "$REPO_ROOT/APP_COMPONENTS.json" --verify
ditto "$BUILD_TEMP/candidate/InkSight-Source" \
  "$APP_PATH/Contents/Resources/InkSight-Source"
ditto "$REPO_ROOT/shared/tools/app/macos/Info.plist" "$APP_PATH/Contents/Info.plist"
swiftc -parse-as-library -target arm64-apple-macosx26.0 -O \
  -framework SwiftUI -framework AppKit \
  "$REPO_ROOT/shared/tools/app/macos/InkSightApp.swift" \
  -o "$APP_PATH/Contents/MacOS/InkSight"

# The local test build is ad-hoc signed for arm64 loading. This is not
# Developer ID signing, notarization, or a Gatekeeper bypass.
codesign --force --deep --sign - "$APP_PATH"
codesign --verify --deep --strict "$APP_PATH"
PYTHONDONTWRITEBYTECODE=1 "$APP_PATH/Contents/Resources/Python/bin/python3.11" -c \
  'import fastapi, uvicorn, PIL, aiosqlite; print("embedded Python and backend wheels: OK")'
codesign --verify --deep --strict "$APP_PATH"

echo "Application: $APP_PATH"
echo "Python: $APP_PATH/Contents/Resources/Python/bin/python3.11"
echo "Source template: $APP_PATH/Contents/Resources/InkSight-Source"
echo "Build scratch retained for audit: $BUILD_TEMP"
