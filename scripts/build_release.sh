#!/bin/bash
# Builds the downloadable, self-contained DJ Crate.app (Apple Silicon) and zips it:
#   scripts/build_release.sh   ->   dist/DJ-Crate-<version>-arm64.zip
#
# The app carries its own Python (python-build-standalone, via uv), DJ Crate and its locked
# dependencies, ffmpeg, ffprobe and deno, so it needs nothing installed. It's ad-hoc signed
# (no paid Apple account), so the first launch needs System Settings → Privacy & Security → Open Anyway.
# GitHub Actions runs this on every version tag (.github/workflows/release.yml).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
source scripts/lib_icon.sh
source scripts/release-deps.env

VERSION="$(djcrate_version "$REPO")"
BUILD="$REPO/build"
DIST="$REPO/dist"
DL="$BUILD/dl"
APP="$BUILD/DJ Crate.app"
RES="$APP/Contents/Resources"
PY="$RES/python/bin/python3"
BUNDLE_ID="io.github.adriatogi.djcrate"
MIN_MACOS="13.0"

step() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
[ "$(uname -s)" = Darwin ] || { echo "Build on a Mac." >&2; exit 1; }
[ "$(uname -m)" = arm64 ] || { echo "Build on an Apple Silicon Mac (the app is arm64)." >&2; exit 1; }
command -v uv >/dev/null || { echo "Needs uv: brew install uv" >&2; exit 1; }

# Download a pinned file into build/dl (kept between builds) and check its SHA-256.
fetch() {
  local url="$1" sha="$2" out="$DL/$3"
  if [ -f "$out" ] && echo "$sha  $out" | shasum -a 256 -c --status; then return; fi
  echo "Downloading $url"
  curl -fL --retry 3 -o "$out.part" "$url"
  echo "$sha  $out.part" | shasum -a 256 -c --status || { echo "Checksum mismatch for $url" >&2; rm -f "$out.part"; exit 1; }
  mv "$out.part" "$out"
}

step "DJ Crate $VERSION"
rm -rf "$APP" "$BUILD/py" "$BUILD/stage"
mkdir -p "$DL" "$DIST" "$APP/Contents/MacOS" "$RES/bin" "$RES/licenses" "$BUILD/stage"

step "Downloading ffmpeg, ffprobe, deno (pinned in scripts/release-deps.env)"
fetch "$FFMPEG_URL" "$FFMPEG_SHA256" "ffmpeg-$FFMPEG_VERSION.zip"
fetch "$FFPROBE_URL" "$FFPROBE_SHA256" "ffprobe-$FFMPEG_VERSION.zip"
fetch "$DENO_URL" "$DENO_SHA256" "deno-$DENO_VERSION.zip"
fetch "$FFMPEG_SRC_URL" "$FFMPEG_SRC_SHA256" "ffmpeg-$FFMPEG_VERSION.tar.xz"
for z in "ffmpeg-$FFMPEG_VERSION.zip" "ffprobe-$FFMPEG_VERSION.zip" "deno-$DENO_VERSION.zip"; do
  ditto -x -k "$DL/$z" "$RES/bin"
done
chmod 755 "$RES/bin/ffmpeg" "$RES/bin/ffprobe" "$RES/bin/deno"

step "Python $PYTHON_VERSION"
uv python install "$PYTHON_VERSION" --install-dir "$BUILD/py" --no-bin
SRC_PY="$(find "$BUILD/py" -maxdepth 1 -type d -name "cpython-$PYTHON_VERSION-macos-aarch64-*" | head -1)"
[ -n "$SRC_PY" ] || { echo "uv didn't install Python $PYTHON_VERSION" >&2; exit 1; }
ditto "$SRC_PY" "$RES/python"
PYLIB="$(echo "$RES"/python/lib/python3.*)"
rm -f "$PYLIB/EXTERNALLY-MANAGED"   # let "Update yt-dlp" use pip later

step "Installing DJ Crate and its locked dependencies"
uv export --frozen --no-dev --no-emit-project --format requirements-txt -o "$BUILD/stage/requirements.txt" >/dev/null
uv pip install --python "$PY" --break-system-packages --require-hashes -r "$BUILD/stage/requirements.txt"
uv pip install --python "$PY" --break-system-packages --no-deps --reinstall-package djcrate "$REPO"
"$PY" -m pip --version >/dev/null   # pip stays: Update yt-dlp needs it

step "Trimming"
rm -rf "$PYLIB"/{test,idlelib,tkinter,turtledemo,turtle.py,ensurepip,lib2to3} \
       "$PYLIB"/lib-dynload/_tkinter* "$RES"/python/lib/{libtcl*,libtk*,tcl*,tk*,itcl*,thread*,pkgconfig} \
       "$RES"/python/include "$RES"/python/share "$PYLIB"/config-3.*-darwin
find "$PYLIB/site-packages" -type d \( -name tests -o -name test \) -prune -exec rm -rf {} +
find "$RES/python" -name __pycache__ -type d -prune -exec rm -rf {} +
# Precompile now, so Python never writes into the signed app (that would break the signature).
"$PY" -m compileall -q -j0 --invalidation-mode unchecked-hash "$PYLIB" >/dev/null || true

step "Launcher, icon, Info.plist, licenses"
clang -O2 -arch arm64 -mmacosx-version-min="$MIN_MACOS" scripts/launcher.c -o "$APP/Contents/MacOS/DJ Crate"
[ -f djcrate/web/icon.png ] || uv run python scripts/make_icon.py
make_icns djcrate/web/icon.png "$RES/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>DJ Crate</string>
  <key>CFBundleDisplayName</key><string>DJ Crate</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>DJ Crate</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>$MIN_MACOS</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>DJ Crate is free software under the GNU GPL v3 or later.</string>
</dict>
</plist>
PLIST
cp LICENSE "$RES/licenses/DJ-Crate-LICENSE.txt"
cp "$PYLIB/LICENSE.txt" "$RES/licenses/Python-LICENSE.txt" 2>/dev/null || true
cat > "$RES/licenses/README.txt" <<TXT
DJ Crate $VERSION is free software: GNU General Public License v3 or later (DJ-Crate-LICENSE.txt).
Source: https://github.com/Adriatogi/DJ-Crate

Included in this app:
- Python $PYTHON_VERSION (PSF License, Python-LICENSE.txt), built by python-build-standalone.
- Python packages in Resources/python/lib/python3.*/site-packages; each one's license is in its
  *.dist-info folder. Among them: yt-dlp (Unlicense), mutagen (GPL-2.0-or-later), pywebview (BSD),
  PyObjC (MIT), FastAPI (MIT), uvicorn (BSD), httpx (BSD), keyring (MIT), Send2Trash (BSD).
- ffmpeg and ffprobe $FFMPEG_VERSION (GPL), static builds from https://ffmpeg.martin-riedl.de.
  The matching ffmpeg source is attached to every DJ Crate release on GitHub, and at $FFMPEG_SRC_URL
- deno $DENO_VERSION (MIT), https://github.com/denoland/deno
TXT

step "Signing (ad-hoc)"
# Every binary must be signed on Apple Silicon. Ones with a real developer signature (e.g. deno)
# keep it; the rest get an ad-hoc one. Deepest first, then the app itself (no --deep).
find "$APP/Contents" -type f -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then
    if codesign -dvv "$f" 2>&1 | grep -q "^Authority="; then continue; fi
    codesign --force --sign - --identifier "$BUNDLE_ID.$(basename "$f" | tr -c 'A-Za-z0-9.\n' '-')" "$f"
  fi
done
codesign --force --sign - --identifier "$BUNDLE_ID" "$APP"
codesign --verify --strict --verbose=1 "$APP"

step "Zipping"
ZIP="$DIST/DJ-Crate-$VERSION-arm64.zip"
rm -f "$ZIP" "$ZIP.sha256"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
(cd "$DIST" && shasum -a 256 "$(basename "$ZIP")" > "$(basename "$ZIP").sha256")
cp "$DL/ffmpeg-$FFMPEG_VERSION.tar.xz" "$DIST/ffmpeg-$FFMPEG_VERSION-source.tar.xz"
du -sh "$APP" "$ZIP"
echo "Built $ZIP"
