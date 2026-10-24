#!/bin/bash
# DJ Crate installer for macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/Adriatogi/DJ-Crate/main/scripts/install.sh | bash
#
# Or from a clone:  scripts/install.sh
#
# Installs what's missing (ffmpeg, uv, a JavaScript runtime) with Homebrew,
# clones the repo to ~/DJ-Crate (override with DJCRATE_DIR), installs Python
# dependencies, and builds "DJ Crate.app" inside that folder. Your library
# (music + crates) also defaults to a Library/ folder in there.

set -euo pipefail

REPO_URL="${DJCRATE_REPO:-https://github.com/Adriatogi/DJ-Crate.git}"
DEST="${DJCRATE_DIR:-$HOME/DJ-Crate}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok() { printf "  \033[32m✓\033[0m %s\n" "$*"; }
step() { printf "\n\033[1;35m▸\033[0m \033[1m%s\033[0m\n" "$*"; }
fail() { printf "\n\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

# The whole script lives in main() so a half-downloaded `curl | bash` can't run partially.
main() {
  bold "DJ Crate installer"
  [ "$(uname)" = "Darwin" ] || fail "DJ Crate only runs on macOS for now."

  step "Homebrew"
  if ! command -v brew >/dev/null 2>&1; then
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [ -x "$b" ] && eval "$("$b" shellenv)" && break
    done
  fi
  command -v brew >/dev/null 2>&1 || fail "Homebrew isn't installed. Install it from https://brew.sh (one command), then run this again."
  ok "brew $(brew --version | head -1 | awk '{print $2}')"

  step "git"
  command -v git >/dev/null 2>&1 || fail "git isn't installed. Run: xcode-select --install   then run this again."
  ok "$(git --version)"

  step "ffmpeg (converts downloads to MP3)"
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "  Installing ffmpeg with Homebrew (takes a minute)…"
    brew install ffmpeg || fail "Couldn't install ffmpeg. Try: brew install ffmpeg"
  fi
  ok "$(ffmpeg -version | head -1 | awk '{print $1, $3}')"

  step "JavaScript runtime (yt-dlp needs one for YouTube)"
  if command -v deno >/dev/null 2>&1; then
    ok "deno $(deno --version | head -1 | awk '{print $2}')"
  elif command -v node >/dev/null 2>&1; then
    ok "node $(node --version)"
  else
    echo "  Installing deno with Homebrew…"
    brew install deno || fail "Couldn't install deno. Try: brew install deno"
    ok "deno $(deno --version | head -1 | awk '{print $2}')"
  fi

  step "uv (Python installer)"
  if ! command -v uv >/dev/null 2>&1; then
    echo "  Installing uv with Homebrew…"
    brew install uv || fail "Couldn't install uv. Try: brew install uv"
  fi
  ok "$(uv --version)"

  step "DJ Crate code"
  local here=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  fi
  if [ -n "$here" ] && [ -f "$here/pyproject.toml" ] && grep -q 'name = "djcrate"' "$here/pyproject.toml"; then
    DEST="$here"
    ok "using this checkout: $DEST"
  elif [ -d "$DEST/.git" ]; then
    git -C "$DEST" pull --ff-only || fail "Couldn't update $DEST (local changes?). Run scripts/update.sh inside it instead."
    ok "updated $DEST"
  elif [ -e "$DEST" ]; then
    fail "$DEST already exists and isn't a DJ Crate checkout. Move it, or set DJCRATE_DIR to another folder."
  else
    git clone "$REPO_URL" "$DEST" || fail "Couldn't clone $REPO_URL"
    ok "cloned to $DEST"
  fi

  step "Python dependencies"
  (cd "$DEST" && uv sync) || fail "Installing dependencies failed. Check your internet connection and run this again."
  ok "installed into $DEST/.venv"

  step "App"
  bash "$DEST/scripts/make_app.sh" || fail "Building the app failed."

  printf "\n"
  bold "Done! DJ Crate.app is in $DEST — open it from there or Spotlight, and drag it to your Dock."
  open -R "$DEST/DJ Crate.app" 2>/dev/null || true
  echo "First launch: pick your library folder, then Settings → Spotify to connect your account."
  echo "To update later: $DEST/scripts/update.sh"
}

main "$@"
