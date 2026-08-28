# Portal behaviour on GNOME 50 / mutter 50

Measured on Fedora 44, GNOME Shell 50.4, Wayland, `xdg-desktop-portal` 1.22.1,
`xdg-desktop-portal-gnome` 50.0. Reproduce with the scripts in `spikes/`.

These are the things that differ from what the specs and docs say. They are the
reason several parts of Scribe look more complicated than they "should".

## GlobalShortcuts: `Deactivated` is never sent

The [spec](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html)
describes `Activated` on key-down and `Deactivated` on key-up, which would make
push-to-talk trivial. **GNOME 50 emits no `Deactivated` at all.** Instead it
forwards keyboard auto-repeat as a stream of `Activated` signals.

Measured over 157 events across 4 physical holds:

| | |
|---|---|
| `Deactivated` events | **0** |
| Initial auto-repeat delay | **500 ms** (4 of 4 holds, exactly) |
| Auto-repeat interval | **30–31 ms** (149 of 157 gaps) |
| Gaps between separate holds | 708 / 908 / 2326 ms |

So one physical hold looks like:

```
key down ──▶ Activated
             ~500 ms of silence          <- indistinguishable from a tap
             Activated ×N every ~30 ms   <- now we know it is held
key up   ──▶ (nothing)
```

A quick tap produces **exactly one** `Activated` and nothing else.

### Consequence

Release has to be inferred from timing, with a two-stage timeout
(`src/portals/shortcuts.py`, `HoldDetector`):

- **Before** any repeat is seen, wait `INITIAL_GAP_MS` (650 ms) — long enough to
  clear the 500 ms repeat delay, so a hold is never cut off at the start.
- **After** a repeat is seen, auto-repeat is confirmed running, so drop to
  `REPEAT_GAP_MS` (120 ms ≈ 4 missed repeats). Release is then detected
  promptly for the normal case of holding the key while speaking.

A tap therefore records for ~650 ms. That is harmless for dictation and doubles
as a trailing buffer that catches the last syllable.

If a compositor ever does send `Deactivated`, `HoldDetector` uses it and switches
the heuristic off permanently, so this degrades to correct behaviour on its own.

This is the same underlying problem as Handy's
[#1539](https://github.com/cjpais/Handy/issues/1539) (push-to-talk rapid toggling
from auto-repeat).

## GlobalShortcuts: GNOME rewrites your preferred trigger

`preferred_trigger` is advisory. Requesting `CTRL+ALT+space` produced a stored
binding of `<Alt>space`, because the user is shown an editor and picks. Always
display the returned `trigger_description` rather than what you asked for — the
raw accelerator is never handed back.

Bindings persist in GSettings under
`org.gnome.settings-daemon.global-shortcuts.application`, keyed by **app ID**
(not session ID), and survive app restarts:

```
io.github.tduarte.Scribe: [('dictate', {'shortcuts': <['<Alt>space']>, ...})]
```

## GlobalShortcuts: `ConfigureShortcuts` exists despite `version = 1`

The interface reports `version = 1`, but `ConfigureShortcuts` (documented as
"added in version 2") is present and callable. Do not gate on the version
property; call it and handle `UnknownMethod`.

## Portal versions actually exported here

| Interface | Version |
|---|---|
| `GlobalShortcuts` | 1 |
| `RemoteDesktop` | 2 (`AvailableDeviceTypes = 7`, `ConnectToEIS` present) |
| `Clipboard` | 1 |
| `Background` | 2 |
| `Notification` | 1 |

## Wayland protocols mutter does *not* implement

Dumped from the compositor's registry (41 globals total):

- **`zwlr_layer_shell_v1` — absent.** No floating always-on-top overlay is
  possible. Combined with GTK4 dropping `set_focus_on_map`, any feedback window
  would steal focus and break paste-back into the original app. Hence Scribe
  uses sound cues plus `Background.SetStatus`, not an overlay.
- **`zwp_virtual_keyboard_manager_v1` — absent.** `wtype` cannot work. With
  `/dev/uinput` (ydotool) unavailable to a sandbox, the RemoteDesktop portal is
  the *only* text-injection route on GNOME.
- `wlr_foreign_toplevel`, `wlr_data_control` — absent.

Present and useful: `xdg_activation_v1`, `zwp_text_input_manager_v3`.

## Non-sandboxed callers

`org.freedesktop.host.portal.Registry` is **not** available on this system, so a
host (non-Flatpak) build cannot declare its app ID that way, and GNOME's backend
rejects app IDs with no installed `.desktop` file. Run the spikes inside the
Flatpak, not on the host.

## RemoteDesktop: consent persists correctly

`persist_mode = 2` plus a stored `restore_token` works as documented. Measured:
the first run showed one consent dialog (with "Allow Remote Interaction" and
"Allow Clipboard Access"); a second run with the cached token showed **no dialog
at all** and went straight to `Start`. The token is re-issued on every `Start`
response, so always overwrite the stored copy.

> Still untested: whether the grant survives a **reboot**. Worth re-running
> `spike-inject` after one.

## RemoteDesktop: Unicode keysyms do NOT work

`NotifyKeyboardKeysym` with the standard Unicode encoding
(`0x01000000 + codepoint`) **silently fails for characters outside the active
keyboard layout.** No D-Bus error is raised — the call succeeds and nothing is
typed.

Sending `Scribe type test: café — 🎙` on a US layout produced:

```
Scribe type test: caf
```

Everything from the `é` onwards was dropped. mutter resolves each keysym against
the current keymap, so this is inherently layout-dependent — the same failure as
Handy's [#439](https://github.com/cjpais/Handy/issues/439).

### Consequence

**Per-character typing is not a viable output mode** and is not offered. Whisper
routinely emits accents, curly quotes and em dashes, so a "type" mode would
truncate real transcriptions at the first non-ASCII character. Scribe ships two
output modes only:

- **paste** (default) — own the selection via the Clipboard portal, then
  synthesize Ctrl+V. Verified to carry accents, em dashes and emoji intact.
- **clipboard** — set the selection and let the user paste.

`ConnectToEIS` + libei could in principle allocate keysyms properly and is the
only route that might restore direct typing. Out of scope.

## Clipboard portal: the compositor pulls, repeatedly

After `SetSelection`, the compositor requests the data via `SelectionTransfer`,
and it may do so **more than once** for a single paste (serials 1 and 2 were both
observed). The handler must therefore be re-entrant and keep the payload
available until the selection is replaced — not free it after the first transfer.

Because the transfer is served on the main loop, **nothing in the paste path may
block**. An early version of `spikes/spike_inject.py` used `time.sleep` between
`SetSelection` and Ctrl+V and could not serve the transfer at all.
