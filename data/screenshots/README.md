# Screenshots

AppStream screenshots for the Flathub listing and for GNOME Software.

## Required files

`data/io.github.tduarte.Scribe.metainfo.xml.in` hard-codes these two names. Rename either file and
the listing breaks with no local error, so keep them exactly as they are:

| File | Caption in the metainfo |
|---|---|
| `main.png` | Dictating into a text editor |
| `models.png` | Choosing a speech recognition model |

`main.png` carries `type="default"` — it is the one shown first and used as the thumbnail. If the
order ever changes, move that attribute with it; exactly one screenshot may have it.

## They are fetched, not installed

The metainfo references them by absolute URL:

```
https://raw.githubusercontent.com/tduarte/Scribe/main/data/screenshots/<file>
```

Meson does not install this directory — nothing here ends up inside the Flatpak. The images are only
ever pulled from GitHub, which has one consequence worth stating plainly: **they must be committed
and pushed to `main` before the URLs resolve.** A branch, a fork or an open PR is not enough.

## Local validation will not catch a missing screenshot

`data/meson.build` runs `appstreamcli validate --no-net`. That flag skips URL resolution entirely, so
the test suite passes whether or not these files exist. Flathub validates *with* network access. To
check the way Flathub does, drop the flag:

```bash
appstreamcli validate --explain build/data/io.github.tduarte.Scribe.metainfo.xml
```

## Capturing

From the [Flathub quality guidelines](https://docs.flathub.org/docs/for-app-authors/metainfo-guidelines/quality-guidelines):

- **Window size 1000x700 or smaller, or 2000x1400 for HiDPI.** This is a cap on the *window*, not
  on the image. A 2x capture of a 916x649 window is 1832x1298 and is well within it. Do not
  downscale such a capture to some other width -- a non-integer scale factor softens all the text.
- **Shoot the app window only.** No wallpaper, no desktop background, no other windows.
- **Keep the native decoration**: title bar, window shadow and rounded corners must all be visible,
  on every edge. Do not maximise the window, which removes the shadow and the rounding.
- **Platform defaults**: default icon theme, fonts, and window controls. Dark mode is allowed but
  must not be the only screenshot.
- **Avoid empty states.** An app with nothing in it reads as unfinished.
- **Captions are one sentence with no full stop** -- see the existing two in the metainfo.
- **3-6 screenshots** is the target for an app of this size. Two is the bare minimum.
- Taken on Linux, PNG.
