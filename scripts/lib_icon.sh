# Shared by make_app.sh and build_release.sh:  make_icns <1024px png> <out.icns>
make_icns() {
  local png="$1" out="$2" set
  set="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$set"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$png" --out "$set/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$png" --out "$set/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$set" -o "$out"
  rm -rf "$(dirname "$set")"
}

# The version, from the one place it's written.
djcrate_version() {
  sed -n 's/^__version__ = "\(.*\)"/\1/p' "$1/djcrate/__init__.py"
}
