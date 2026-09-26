#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

PROJECT_DIR="$HOME/yt-downloader"
VENV="$PROJECT_DIR/venv/bin/python"

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

echo "📦 Installing/updating PyInstaller..."
"$VENV" -m pip install -U pyinstaller

echo "🧹 Cleaning previous build..."
rm -rf build dist "Media Toolkit.spec"

echo "🔨 Building Media Toolkit.app..."
"$VENV" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "Media Toolkit" \
  --add-binary "$FFMPEG_PATH:." \
  --add-binary "$FFPROBE_PATH:." \
  main.py

echo
if [ -d "dist/Media Toolkit.app" ]; then
  echo "🎉 BUILD COMPLETE"
  echo "📍 dist/Media Toolkit.app"
  echo
  echo "Test it with:"
  echo 'open "dist/Media Toolkit.app"'
else
  echo "❌ Build finished but the .app was not found."
  exit 1
fi
