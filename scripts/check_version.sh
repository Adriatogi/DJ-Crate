#!/bin/bash
# CI: the pushed tag (v1.2.3) must match __version__ and have a CHANGELOG section.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO/scripts/lib_icon.sh"
TAG="${1:?usage: check_version.sh vX.Y.Z}"
VERSION="$(djcrate_version "$REPO")"
BASE="${TAG#v}"; BASE="${BASE%%-*}"   # v1.2.0-rc1 is a pre-release of 1.2.0
[ "$BASE" = "$VERSION" ] || { echo "Tag $TAG doesn't match __version__ $VERSION in djcrate/__init__.py" >&2; exit 1; }
grep -q "^## ${VERSION}\b" "$REPO/CHANGELOG.md" || { echo "CHANGELOG.md has no '## $VERSION' section" >&2; exit 1; }
echo "Version $VERSION OK"
