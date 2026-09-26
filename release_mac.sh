#!/bin/bash
set -euo pipefail

# ============================================================
# MEDIA TOOLKIT — MAC RELEASE BUILDER
# Builds: Media Toolkit.app + Media Toolkit.dmg
# Optional: Developer ID signing + Apple notarization
# ============================================================

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

APP_NAME="Media Toolkit"
PYTHON="${PROJECT_DIR}/venv/bin/python"
DIST_DIR="${PROJECT_DIR}/dist"
BUILD_DIR="${PROJECT_DIR}/build"
ICONSET_DIR="${PROJECT_DIR}/Media Toolkit.iconset"
ICON_FILE="${PROJECT_DIR}/Media Toolkit.icns"
APP_PATH="${DIST_DIR}/${APP_NAME}.app"
DMG_PATH="${DIST_DIR}/${APP_NAME}.dmg"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "❌ This release script must be run on macOS."
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "❌ Could not find project venv: $PYTHON"
  echo "Create it with: python3 -m venv venv"
  exit 1
fi

if [[ ! -f "main.py" ]]; then
  echo "❌ main.py not found in $PROJECT_DIR"
  exit 1
fi

FFMPEG_PATH="$(command -v ffmpeg || true)"
FFPROBE_PATH="$(command -v ffprobe || true)"

if [[ -z "$FFMPEG_PATH" || -z "$FFPROBE_PATH" ]]; then
  echo "❌ FFmpeg/ffprobe not found on PATH."
  echo "Install with: brew install ffmpeg"
  exit 1
fi

ARCH="$(uname -m)"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  MEDIA TOOLKIT — RELEASE BUILD"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Architecture : $ARCH"
echo "  FFmpeg       : $FFMPEG_PATH"
echo "  ffprobe      : $FFPROBE_PATH"
echo ""

# ------------------------------------------------------------
# Dependencies
# ------------------------------------------------------------

echo "📦 Checking PyInstaller..."
"$PYTHON" -m pip install -U pyinstaller >/dev/null

# ------------------------------------------------------------
# App icon
# ------------------------------------------------------------

if [[ ! -f "$ICON_FILE" ]]; then
  if [[ ! -f "make_icon.py" ]]; then
    echo "❌ make_icon.py is missing."
    exit 1
  fi

  echo "🎨 Creating app icon..."
  rm -rf "$ICONSET_DIR" "$ICON_FILE"
  "$PYTHON" make_icon.py
fi

# ------------------------------------------------------------
# Clean build
# ------------------------------------------------------------

echo "🧹 Cleaning previous build..."
rm -rf "$BUILD_DIR" "$DIST_DIR" "${APP_NAME}.spec"

# ------------------------------------------------------------
# Build .app
# ------------------------------------------------------------

echo "🔨 Building ${APP_NAME}.app..."
"$PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "$APP_NAME" \
  --icon "$ICON_FILE" \
  --add-binary "$FFMPEG_PATH:." \
  --add-binary "$FFPROBE_PATH:." \
  main.py

if [[ ! -d "$APP_PATH" ]]; then
  echo "❌ App build failed."
  exit 1
fi

echo "✅ App built: $APP_PATH"

# ------------------------------------------------------------
# Optional Developer ID signing
# ------------------------------------------------------------

if [[ -n "${DEVELOPER_ID_APPLICATION:-}" ]]; then
  echo "🔐 Signing with Developer ID..."
  codesign \
    --deep \
    --force \
    --verbose \
    --options runtime \
    --timestamp \
    --sign "$DEVELOPER_ID_APPLICATION" \
    "$APP_PATH"

  codesign --verify --deep --strict --verbose=2 "$APP_PATH"
  echo "✅ Code signature verified."
else
  echo "ℹ️  Skipping Developer ID signing."
  echo "    To sign, export DEVELOPER_ID_APPLICATION with your certificate name."
fi

# ------------------------------------------------------------
# Create professional DMG layout
# ------------------------------------------------------------

echo "💿 Creating DMG..."
DMG_ROOT="$(mktemp -d)"
trap 'rm -rf "$DMG_ROOT"' EXIT

cp -R "$APP_PATH" "$DMG_ROOT/"
ln -s /Applications "$DMG_ROOT/Applications"

rm -f "$DMG_PATH"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$DMG_ROOT" \
  -ov \
  -format UDZO \
  "$DMG_PATH" >/dev/null

echo "✅ DMG built: $DMG_PATH"

# ------------------------------------------------------------
# Optional notarization
# ------------------------------------------------------------

if [[ -n "${NOTARY_PROFILE:-}" ]]; then
  if ! xcrun --find notarytool >/dev/null 2>&1; then
    echo "❌ notarytool is unavailable. Install/select Xcode or Command Line Tools."
    exit 1
  fi

  echo "☁️  Submitting DMG for Apple notarization..."
  xcrun notarytool submit "$DMG_PATH" \
    --keychain-profile "$NOTARY_PROFILE" \
    --wait

  echo "📌 Stapling notarization ticket..."
  xcrun stapler staple "$DMG_PATH"
  xcrun stapler validate "$DMG_PATH"

  echo "✅ Notarization + stapling complete."
else
  echo "ℹ️  Skipping notarization."
  echo "    To notarize, export NOTARY_PROFILE with your notarytool keychain profile."
fi

# ------------------------------------------------------------
# Final output
# ------------------------------------------------------------

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🎉 RELEASE READY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  App : $APP_PATH"
echo "  DMG : $DMG_PATH"
echo ""
echo "  Test:"
echo "    open \"$APP_PATH\""
echo ""
echo "  Send editors:"
echo "    $DMG_PATH"
echo ""
