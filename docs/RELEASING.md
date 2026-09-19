# Releasing

Pushing a `v*` tag runs [release.yml](../.github/workflows/release.yml). It
builds the tag from the Flathub manifest, signs it, and publishes it in two
places:

- a Flatpak repository on GitHub Pages, at `https://tduarte.github.io/Scribe/`,
  which is what installs update from;
- `scribe.flatpak` on the GitHub release for the tag.

The site is rebuilt whole on every release and holds one commit per ref, so it
does not grow. At 0.1.2 the app and its debug symbols come to about 130 MB of
Pages' 1 GB.

## One-time setup

### Signing key

Clients trust the repository through a GPG key whose public half is embedded in
`scribe.flatpakref`. Use a key made for this and nothing else. It has no
passphrase because CI has nobody to type one.

```bash
export GNUPGHOME="$(mktemp -d)"
gpg --batch --passphrase '' --quick-generate-key \
    "Scribe releases <tduarte.personal@gmail.com>" ed25519 sign never
gpg --armor --export-secret-keys > scribe-signing-key.asc
gh secret set FLATPAK_GPG_PRIVATE_KEY < scribe-signing-key.asc
```

Store `scribe-signing-key.asc` somewhere offline, then delete the local copy and
the temporary `GNUPGHOME`. If the key is lost, the next release has to be signed
with a new one, and every existing install must remove and re-add the remote to
keep updating.

### GitHub Pages

Set the Pages source to GitHub Actions, under Settings → Pages, or:

```bash
gh api -X POST repos/tduarte/Scribe/pages -f build_type=workflow
```

The `github-pages` environment that this creates only accepts deployments from
the default branch, and a release deploys from a tag. Under Settings →
Environments → github-pages → Deployment branches and tags, add a tag rule for
`v*`, or:

```bash
gh api -X POST repos/tduarte/Scribe/environments/github-pages/deployment-branch-policies \
    -f name='v*' -f type=tag
```

Without the rule the `pages` job fails with "Tag "v…" is not allowed to deploy
to github-pages due to environment protection rules".

## Cutting a release

1. Set the version in `meson.build` and add the `<release>` entry to
   `data/io.github.tduarte.Scribe.metainfo.xml.in`. The workflow refuses a tag
   that does not match `meson.build`.
2. Merge to `main` and let CI finish. The release build reuses CI's build cache
   when the tag is on a commit CI has built, which skips the twenty-minute
   whisper.cpp compile.
3. Tag and push:

   ```bash
   git tag -a v0.1.3 -m "Scribe 0.1.3"
   git push origin v0.1.3
   ```

The manifest pins a tag and commit for Flathub. The workflow rewrites both to
the tag being built, so the file does not need updating first.

If the GitHub release for the tag already exists, the bundle is added to it.
Otherwise one is created with generated notes.

To run a release again, open Actions → Release → Run workflow and choose the tag
under "Use workflow from".

## Checking a release

```bash
flatpak remote-ls --user scribe
flatpak update --user io.github.tduarte.Scribe
```

On a machine that has never had it:

```bash
flatpak install --user https://tduarte.github.io/Scribe/scribe.flatpakref
```

## Testing the publishing step locally

[publish-repo.sh](../build-aux/publish-repo.sh) is the part of the workflow that
turns a signed repo into the site, and runs anywhere. With a throwaway key in a
temporary `GNUPGHOME`:

```bash
flatpak-builder --repo=repo --default-branch=stable --gpg-sign=$KEY \
    --force-clean build build-aux/scribe-dev.yaml
build-aux/publish-repo.sh repo site http://localhost:8765 $KEY
python3 -m http.server 8765 --directory site
```

Install from it into a scratch Flatpak directory, leaving the real one alone:

```bash
FLATPAK_USER_DIR=/tmp/fp-test flatpak --user install --no-deps \
    http://localhost:8765/scribe.flatpakref
```
