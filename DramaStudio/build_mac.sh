#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Build macOS must run on a Mac." >&2
  exit 1
fi

cd "$(dirname "$0")"
mkdir -p build/mac-icon.iconset releases

if [[ -z "${FFMPEG_BIN:-}" ]]; then
  case "$(uname -m)" in
    arm64) ffmpeg_asset='ffmpeg-macos-arm64.tar.gz' ;;
    x86_64) ffmpeg_asset='ffmpeg-macos-x64.tar.gz' ;;
    *) echo "Unsupported Mac architecture: $(uname -m)" >&2; exit 1 ;;
  esac
  ffmpeg_release='ffmpeg-2026.06.11'
  ffmpeg_url="https://github.com/vanloctech/ffmpeg-macos/releases/download/${ffmpeg_release}"
  mkdir -p build/mac-ffmpeg
  if [[ ! -x build/mac-ffmpeg/ffmpeg ]]; then
    curl -fL --retry 3 "$ffmpeg_url/$ffmpeg_asset" -o "build/mac-ffmpeg/$ffmpeg_asset"
    curl -fL --retry 3 "$ffmpeg_url/$ffmpeg_asset.sha256" -o "build/mac-ffmpeg/$ffmpeg_asset.sha256"
    (cd build/mac-ffmpeg && shasum -a 256 -c "$ffmpeg_asset.sha256")
    tar -xzf "build/mac-ffmpeg/$ffmpeg_asset" -C build/mac-ffmpeg
    ffmpeg_found=$(find build/mac-ffmpeg -type f -name ffmpeg -print -quit)
    if [[ -z "$ffmpeg_found" ]]; then
      echo "FFmpeg archive did not contain an ffmpeg executable." >&2
      exit 1
    fi
    if [[ "$ffmpeg_found" != 'build/mac-ffmpeg/ffmpeg' ]]; then
      cp "$ffmpeg_found" build/mac-ffmpeg/ffmpeg
    fi
    chmod +x build/mac-ffmpeg/ffmpeg
  fi
  FFMPEG_BIN="$PWD/build/mac-ffmpeg/ffmpeg"
fi
if [[ ! -x "$FFMPEG_BIN" ]]; then
  echo "FFMPEG_BIN does not point to an executable file." >&2
  exit 1
fi
if [[ "$(basename "$FFMPEG_BIN")" != "ffmpeg" ]]; then
  echo "FFMPEG_BIN must be named ffmpeg so the app can find it." >&2
  exit 1
fi
if otool -L "$FFMPEG_BIN" | tail -n +2 | grep -Evq '^[[:space:]]+(/usr/lib/|/System/Library/)'; then
  echo "FFmpeg depends on non-system libraries. Provide a self-contained build." >&2
  exit 1
fi
if ! "$FFMPEG_BIN" -hide_banner -filters | awk '$2 == "ass" { found = 1 } END { exit !found }'; then
  echo "This FFmpeg build lacks the ass subtitle filter." >&2
  exit 1
fi

for size in 16 32 128 256 512; do
  sips -s format png -z "$size" "$size" assets/dramastudio-icon-v2.png \
    --out "build/mac-icon.iconset/icon_${size}x${size}.png" >/dev/null
done
for size in 16 32 128 256 512; do
  doubled=$((size * 2))
  sips -s format png -z "$doubled" "$doubled" assets/dramastudio-icon-v2.png \
    --out "build/mac-icon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns build/mac-icon.iconset -o build/dramastudio.icns

python3 -m PyInstaller --noconfirm --clean --onedir --windowed \
  --name DramaStudio \
  --osx-bundle-identifier com.toannt.dramastudio \
  --icon build/dramastudio.icns \
  --distpath build/mac-dist \
  --workpath build/mac-work \
  --specpath packaging \
  --add-data 'studio_ui.html:.' \
  --add-data 'assets/dramastudio-icon-v2.png:assets' \
  --add-data 'assets/dramastudio-icon-v2.ico:assets' \
  --add-binary "$FFMPEG_BIN:." \
  --collect-all webview \
  --collect-all ctranslate2 \
  --collect-all sentencepiece \
  --collect-all faster_whisper \
  --collect-all av \
  phimora_ui.py

if [[ -d build/mac-dist/DramaStudio.app/Contents/MacOS/models ||
      -d build/mac-dist/DramaStudio.app/Contents/Resources/models ||
      -d build/mac-dist/DramaStudio.app/Contents/Frameworks/models ]]; then
  echo "The app bundle unexpectedly contains models." >&2
  exit 1
fi

rm -rf build/mac-dmg
mkdir -p build/mac-dmg
cp -R build/mac-dist/DramaStudio.app build/mac-dmg/DramaStudio.app
ln -sfn /Applications build/mac-dmg/Applications
hdiutil create -volname 'DramaStudio 1.0.0' \
  -srcfolder build/mac-dmg \
  -ov -format UDZO "releases/DramaStudio-macOS-$(uname -m)-v1.0.0.dmg"

echo "Created releases/DramaStudio-macOS-$(uname -m)-v1.0.0.dmg"
