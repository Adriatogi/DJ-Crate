#!/bin/bash
# Checks a built release zip actually works, the way a user would get it:
#   scripts/smoke_release.sh dist/DJ-Crate-<version>-arm64.zip
# Unzips it, checks the signature, runs the bundled tools, starts the server headless
# (no window, no browser) on a spare port with throwaway settings, and checks nothing
# was written inside the app.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO/scripts/lib_icon.sh"
ZIP="${1:?usage: smoke_release.sh <zip>}"
VERSION="$(djcrate_version "$REPO")"
TMP="$(mktemp -d)"
PORT=18765
SERVER=""
cleanup() { [ -n "$SERVER" ] && kill "$SERVER" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT
ok() { printf "  \033[32m✓\033[0m %s\n" "$*"; }
die() { printf "  \033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

ditto -x -k "$ZIP" "$TMP/unzipped"
APP="$TMP/unzipped/DJ Crate.app"
RES="$APP/Contents/Resources"
PY="$RES/python/bin/python3"
[ -d "$APP" ] || die "no DJ Crate.app in the zip"
touch "$TMP/marker"

codesign --verify --strict "$APP" || die "signature broken"
ok "signature valid"

env -i HOME="$TMP/home" PATH=/usr/bin:/bin DJCRATE_BUNDLE="$APP" PYTHONNOUSERSITE=1 "$PY" -c "
import djcrate, webview, AppKit, WebKit, yt_dlp, mutagen, fastapi, keyring, send2trash, certifi
from djcrate import config, tools
assert djcrate.__version__ == '$VERSION', djcrate.__version__
assert config.BUNDLED and tools.ffmpeg_dir() == '$RES/bin', tools.ffmpeg_dir()
assert tools.js_runtime() == 'deno'
print(djcrate.__version__)" >/dev/null || die "bundled Python can't import DJ Crate"
ok "DJ Crate $VERSION imports, finds the bundled ffmpeg and deno (with an empty PATH)"

"$RES/bin/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -q libmp3lame || die "ffmpeg has no MP3 encoder"
"$RES/bin/ffprobe" -version >/dev/null || die "ffprobe doesn't run"
"$RES/bin/deno" --version >/dev/null || die "deno doesn't run"
"$PY" -m yt_dlp --version >/dev/null || die "yt-dlp doesn't run"
"$PY" -m pip --version >/dev/null || die "pip missing (Update yt-dlp needs it)"
ok "ffmpeg (libmp3lame), ffprobe, deno, yt-dlp and pip run"

mkdir -p "$TMP/support" "$TMP/logs" "$TMP/home"
echo "{\"port\": $PORT}" > "$TMP/support/config.json"
env -i HOME="$TMP/home" PATH=/usr/bin:/bin DJCRATE_APP_SUPPORT="$TMP/support" DJCRATE_LOG_DIR="$TMP/logs" \
  DJCRATE_LIBRARIES_DIR="$TMP/libs" DJCRATE_BUNDLE="$APP" PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -m djcrate.main --headless >"$TMP/server.log" 2>&1 &
SERVER=$!
for _ in $(seq 1 100); do
  curl -sf "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1 && break
  kill -0 "$SERVER" 2>/dev/null || { cat "$TMP/server.log"; die "server exited"; }
  sleep 0.2
done
curl -sf "http://127.0.0.1:$PORT/api/ping" | grep -q "\"version\":\"$VERSION\"" || { cat "$TMP/server.log"; die "/api/ping failed"; }
STATUS="$(curl -sf "http://127.0.0.1:$PORT/api/status")"
echo "$STATUS" | grep -q '"install":"app"' || die "status doesn't say it's the app: $STATUS"
echo "$STATUS" | grep -q '"ffmpeg":true' || die "server can't find ffmpeg"
echo "$STATUS" | grep -q '"js":"deno"' || die "server can't find deno"
curl -sf "http://127.0.0.1:$PORT/" | grep -q "theme.css" || die "web page not served"
ok "server starts, serves the app, finds its tools"
kill "$SERVER"; wait "$SERVER" 2>/dev/null || true; SERVER=""

[ -z "$(find "$APP" -newer "$TMP/marker" -not -type d | head -1)" ] || { find "$APP" -newer "$TMP/marker" -not -type d | head; die "files were written inside the app"; }
codesign --verify --strict "$APP" || die "signature broke while running"
ok "nothing written inside the app; signature still valid"
echo "Smoke test passed: $(basename "$ZIP")"
