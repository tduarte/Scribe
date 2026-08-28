#!/usr/bin/env bash
# Run flatpak-builder-lint over an exported repo, tolerating the two findings
# that cannot be cleared outside Flathub's own build infrastructure.
#
# Screenshots are mirrored to dl.flathub.org by Flathub's builders. Locally and
# in CI, appstream-compose finds no media directory to commit ("Media directory
# does not exist, skipping commit"), so both checks below fail no matter what
# the app does. Every other finding is a real failure.
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
