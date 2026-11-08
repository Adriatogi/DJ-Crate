#!/bin/bash
# Builds "DJ Crate.app" inside this folder (next to the code) so you can launch
# it from Finder, Spotlight or the Dock. The app just runs this repo's venv python.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$REPO/DJ Crate.app"

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "No .venv yet — run: cd \"$REPO\" && uv sync" >&2
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# App icon: PNG -> iconset -> .icns (sips/iconutil ship with macOS)
ICON_PNG="$REPO/djcrate/web/icon.png"
[ -f "$ICON_PNG" ] || python3 "$REPO/scripts/make_icon.py"
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$ICON_PNG" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$ICON_PNG" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
rm -rf "$(dirname "$ICONSET")"

cat > "$APP/Contents/MacOS/DJ Crate" <<LAUNCH
#!/bin/bash
# Finder gives apps a bare PATH; add Homebrew + uv so ffmpeg is found.
export PATH="/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:\$PATH"
cd "$REPO"
exec "$REPO/.venv/bin/python" -m djcrate.main
LAUNCH
chmod +x "$APP/Contents/MacOS/DJ Crate"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>DJ Crate</string>
  <key>CFBundleDisplayName</key><string>DJ Crate</string>
  <key>CFBundleIdentifier</key><string>local.djcrate</string>
  <key>CFBundleExecutable</key><string>DJ Crate</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

touch "$APP"  # nudge Finder to pick up the new icon
echo "Built: $APP"
