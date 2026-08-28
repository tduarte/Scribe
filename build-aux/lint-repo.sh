#!/usr/bin/env bash
# Run flatpak-builder-lint over an exported repo, tolerating the two findings
# that cannot be cleared outside Flathub's own build infrastructure.
#
# All three findings below are about mirroring assets to dl.flathub.org, which
# only Flathub's own builders can write to.
#
#   appstream-external-screenshot-url        the catalog points at our GitHub
#                                            raw URLs, not the Flathub mirror
#   appstream-screenshots-not-mirrored-in-ostree
#                                            appstream-compose found no media to
#                                            commit ("Media directory does not
#                                            exist, skipping commit")
#   appstream-remote-icon-not-mirrored       induced BY asking for mirroring:
#                                            with --mirror-screenshots-url the
#                                            catalog rewrites the icon to a
#                                            dl.flathub.org URL we cannot fill.
#                                            Build without the mirror flags and
#                                            it does not appear at all.
#
# Every other finding is a real failure.
#
# Usage: build-aux/lint-repo.sh <repo-dir>
set -uo pipefail

repo="${1:-repo}"

if command -v flatpak-builder-lint >/dev/null 2>&1; then
  out="$(flatpak-builder-lint repo "$repo" 2>&1)"
else
  out="$(flatpak run --command=flatpak-builder-lint org.flatpak.Builder repo "$repo" 2>&1)"
fi

echo "$out"

python3 - "$out" <<'PY'
import json, sys

INFRA_ONLY = {
    "appstream-external-screenshot-url",
    "appstream-screenshots-not-mirrored-in-ostree",
    "appstream-remote-icon-not-mirrored",
}

raw = sys.argv[1].strip()
if not raw:
    print("lint-repo: clean")
    sys.exit(0)

try:
    report = json.loads(raw)
except ValueError:
    print("lint-repo: could not parse linter output as JSON, treating as failure")
    sys.exit(1)

errors = set(report.get("errors", []))
waived = errors & INFRA_ONLY
real = errors - INFRA_ONLY

for name in sorted(waived):
    print(f"lint-repo: waived (Flathub mirrors screenshots itself): {name}")
for name in sorted(real):
    print(f"lint-repo: ERROR {name}")

sys.exit(1 if real else 0)
PY
