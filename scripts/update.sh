#!/bin/bash
# Update DJ Crate to the latest version:  scripts/update.sh
#
# Pulls the latest code, reinstalls dependencies and rebuilds the app. If
# anything fails, it rolls back to the version you had, so you're never left
# with a broken install. Your library (music + crates) is never touched.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

ok() { printf "  \033[32m✓\033[0m %s\n" "$*"; }
fail() { printf "\n\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

for b in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
  case ":$PATH:" in *":$b:"*) ;; *) PATH="$b:$PATH" ;; esac
done
command -v uv >/dev/null 2>&1 || fail "uv isn't installed. Run scripts/install.sh first."

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  fail "You have local code changes in $REPO. Commit or stash them, then run this again."
fi

PREV="$(git rev-parse HEAD)"
echo "Checking for updates…"
git pull --ff-only || fail "Couldn't download the update (offline, or your branch has diverged). Nothing changed."
NEW="$(git rev-parse HEAD)"

rollback() {
  echo "Update failed — going back to the version you had…"
  git reset --hard "$PREV" >/dev/null
  uv sync >/dev/null 2>&1 || true
  bash scripts/make_app.sh >/dev/null 2>&1 || true
  fail "Update failed; you're still on the previous version (${PREV:0:7})."
}

uv sync || rollback
bash scripts/make_app.sh || rollback

if [ "$PREV" = "$NEW" ]; then
  ok "Already up to date (${NEW:0:7})."
else
  ok "Updated ${PREV:0:7} → ${NEW:0:7}:"
  git log --oneline "$PREV..$NEW" | sed 's/^/    /'
  echo "Quit and reopen DJ Crate to use the new version."
fi
