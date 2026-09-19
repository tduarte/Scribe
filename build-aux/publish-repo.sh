#!/usr/bin/env bash
# Turn a repo exported by flatpak-builder into a static site that Flatpak can
# install and update from: the OSTree repo itself, a .flatpakref for the app, a
# .flatpakrepo for the remote, and a landing page.
#
# The refs in the repo must already be signed (flatpak-builder --gpg-sign). This
# signs what is generated here -- the summary and the appstream branch -- with
# the same key, and embeds its public half in the two descriptor files, which
# is how a client comes to trust the remote.
#
# From-scratch static deltas are generated because a first install otherwise
# fetches every object as its own HTTP request, which is slow on a static host.
#
# The secret key is looked up in GNUPGHOME, or ~/.gnupg when that is unset.
#
# Usage: build-aux/publish-repo.sh <repo-dir> <site-dir> <base-url> <key-fingerprint>
set -euo pipefail

repo="$1"
site="$2"
url="${3%/}"
key="$4"

app=io.github.tduarte.Scribe
branch=stable
homepage=https://github.com/tduarte/Scribe
summary="Dictate anywhere with your voice"
here="$(dirname "$0")"

gpg_home=()
if [ -n "${GNUPGHOME:-}" ]; then
  gpg_home=(--gpg-homedir="$GNUPGHOME")
fi

flatpak build-update-repo "$repo" \
  --title=Scribe --comment="$summary" --homepage="$homepage" \
  --default-branch="$branch" \
  --gpg-sign="$key" "${gpg_home[@]}" \
  --generate-static-deltas --prune

rm -rf "$site"
mkdir -p "$site"
cp -a "$repo" "$site/repo"

pubkey="$(gpg --batch --export "$key" | base64 -w0)"
if [ -z "$pubkey" ]; then
  echo "publish-repo: no public key found for $key" >&2
  exit 1
fi

cat > "$site/scribe.flatpakref" <<EOF
[Flatpak Ref]
Title=Scribe
Name=$app
Branch=$branch
Url=$url/repo
SuggestRemoteName=scribe
Homepage=$homepage
Comment=$summary
IsRuntime=false
RuntimeRepo=https://dl.flathub.org/repo/flathub.flatpakrepo
GPGKey=$pubkey
EOF

cat > "$site/scribe.flatpakrepo" <<EOF
[Flatpak Repo]
Title=Scribe
Url=$url/repo
Homepage=$homepage
Comment=$summary
DefaultBranch=$branch
GPGKey=$pubkey
EOF

sed "s|@URL@|$url|g" "$here/pages/index.html" > "$site/index.html"

echo "publish-repo: wrote $site for $url"
