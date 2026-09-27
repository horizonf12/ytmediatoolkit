#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

PROJECT_DIR="$HOME/yt-downloader"
VENV="$PROJECT_DIR/venv/bin/python"
APP_NAME="Media Toolkit"
ICON="$PROJECT_DIR/Media Toolkit.icns"

if [ ! -x "$VENV" ]; then
  echo "❌ Could not find $VENV"
  echo "Run: python3 -m venv ~/yt-downloader/venv"
  exit 1
fi

if [ ! -f main.py ]; then
  echo "❌ main.py not found in $PWD"
  exit 1
fi

FFMPEG_PATH="$(command -v ffmpeg || true)"
FFPROBE_PATH="$(command -v ffprobe || true)"

if [ -z "$FFMPEG_PATH" ] || [ -z "$FFPROBE_PATH" ]; then
  echo "❌ FFmpeg/ffprobe not found on PATH."
  echo "Install them with: brew install ffmpeg"
  exit 1
fi

echo "✅ Python:  $VENV"
echo "✅ FFmpeg: $FFMPEG_PATH"
echo "✅ ffprobe: $FFPROBE_PATH"

echo "📦 Updating PyInstaller..."
"$VENV" -m pip install -U pyinstaller

echo "🧹 Cleaning previous release build..."
rm -rf build dist "${APP_NAME}.spec" dmg_contents releases
mkdir -p releases

echo "🔨 Building ${APP_NAME}.app..."
"$VENV" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "$APP_NAME" \
  --icon "$ICON" \
  --add-binary "$FFMPEG_PATH:." \
  --add-binary "$FFPROBE_PATH:." \
  main.py

APP_PATH="dist/${APP_NAME}.app"

if [ ! -d "$APP_PATH" ]; then
  echo "❌ ${APP_NAME}.app was not created."
  exit 1
fi

echo "✅ App built: $APP_PATH"

echo "🧪 Opening app for final manual test..."
open "$APP_PATH"

echo
echo "After confirming the app works, press Enter here to create the DMG."
read -r

echo "📀 Creating DMG..."
mkdir -p dmg_contents
cp -R "$APP_PATH" "dmg_contents/"
ln -s /Applications "dmg_contents/Applications"

hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "dmg_contents" \
  -ov \
  -format UDZO \
  "releases/${APP_NAME}.dmg"

DMG_PATH="releases/${APP_NAME}.dmg"

echo
echo "🎉 RELEASE READY"
echo "📦 App: $APP_PATH"
echo "💿 DMG: $DMG_PATH"
echo "📏 DMG size: $(du -h "$DMG_PATH" | awk '{print $1}')"
echo
echo "Install locally with:"
echo "  open \"$DMG_PATH\""
echo
echo "Upload the DMG to a GitHub Release rather than committing it to the repository."
