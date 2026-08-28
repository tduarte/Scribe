#!/usr/bin/env python3
"""Does Clipboard.SelectionTransfer mean "an app pasted", or just "we took the
clipboard"?

Everything in the escalating-paste design rests on the answer. The idea is to
send a paste chord, wait, and try a different chord only if nothing read the
clipboard -- which works only if a transfer is a *paste receipt*. If the
compositor pulls the payload eagerly the moment we call SetSelection, the signal
carries no information and the design is dead.

docs/PORTAL-FINDINGS.md records that a single paste can produce several
SelectionTransfer calls, but not whether one arrives when nobody pastes at all.

Modes:

  idle    Own the selection, send no chord, wait. Needs nobody at the keyboard.
          ANY transfer here means the signal is eager -> the design fails.

  chord CHORD
          Own the selection, send CHORD, and report whether a transfer arrives
          and how long it took. Focus a window during the countdown first.
          CHORD is one of: paste-key, ctrl-v, ctrl-shift-v, shift-insert.

The clipboard is saved before the test and put back afterwards.
"""

from __future__ import annotations

import json
import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

sys.path.insert(0, "/app/share/scribe/scribe")
from portals.base import PORTAL_BUS, PORTAL_PATH  # noqa: E402
from portals.inject import CB, TextInjector  # noqa: E402

TEXT = "Scribe ladder probe"
IDLE_WAIT_MS = 3000
CHORD_WAIT_MS = 2000
COUNTDOWN_S = 5

TOKEN_FILE = os.path.join(
    GLib.get_user_config_dir(), "scribe-spike-restore-token.json"
)


def load_token() -> str:
    try:
        with open(TOKEN_FILE) as fh:
            return json.load(fh).get("restore_token") or ""
    except (OSError, ValueError):
        return ""


def save_token(token: str) -> None:
    if not token:
        return
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as fh:
        json.dump({"restore_token": token}, fh)


class Probe:
    def __init__(self, loop: GLib.MainLoop, mode: str, chord: str, gap_ms: int) -> None:
        self.loop = loop
        self.mode = mode
        self.chord = chord
        self.gap_ms = gap_ms
        self.transfers: list[tuple[float, str]] = []
        self.t0 = 0.0
        self.baseline = 0
        self.sent_at = 0.0
        self.saved: bytes | None = None

        self.inj = TextInjector(
            get_token=load_token,
            set_token=save_token,
            on_state_change=lambda s, d="": print(f"  session: {s.name.lower()} {d}".rstrip()),
        )
        # Watch the raw signal ourselves. TextInjector consumes it to serve the
        # payload; we only want the arrival times, so a second subscriber keeps
        # the measurement independent of what the injector does with it.
        self.inj.portal.bus.signal_subscribe(
            PORTAL_BUS, CB, "SelectionTransfer", PORTAL_PATH,
            None, 0, self._on_transfer,
        )

    def _on_transfer(self, _c, _s, _p, _i, _sig, params) -> None:
        session, mime_type, serial = params.unpack()
        if session != self.inj.session:
            return
        dt = (time.monotonic() - self.t0) * 1000 if self.t0 else -1
        self.transfers.append((dt, mime_type))
        print(f"    SelectionTransfer  +{dt:7.1f} ms  serial={serial} {mime_type}")

    # --- flow ---------------------------------------------------------------

    def run(self) -> None:
        self.inj.ensure_session(self._ready)

    def _ready(self, ok: bool) -> None:
        if not ok:
            print("  FAILED: no RemoteDesktop session")
            return self._quit()
        if not self.inj.clipboard_enabled:
            print("  FAILED: clipboard access was not granted")
            return self._quit()
        print("  saving the current clipboard first")
        self.inj._read_selection(self._saved_then_go)

    def _saved_then_go(self, data: bytes | None) -> None:
        self.saved = data
        n = len(data) if data else 0
        print(f"  saved {n} bytes of existing clipboard")
        if self.mode == "idle":
            self._run_idle()
        else:
            self._countdown(COUNTDOWN_S)

    def _run_idle(self) -> None:
        print()
        print("  Owning the selection and sending NO chord.")
        print("  Nothing should read it. Any transfer below is the eager case.")
        self.t0 = time.monotonic()
        self.inj._own_selection(TEXT.encode())
        GLib.timeout_add(IDLE_WAIT_MS, self._report)

    def _countdown(self, left: int) -> None:
        if left:
            print(f"  focus a window... {left}", flush=True)
            GLib.timeout_add(1000, lambda: self._countdown(left - 1))
            return
        print()
        print(f"  Owning the selection, then sending {self.chord!r}.")
        self.t0 = time.monotonic()
        self.inj._own_selection(TEXT.encode())
        GLib.timeout_add(120, self._send)

    def _send(self) -> bool:
        # The compositor pulls the payload once, eagerly, ~2 ms after
        # SetSelection and with nothing pasting. That is the baseline; only
        # transfers on top of it mean an application actually read the
        # clipboard. Comparing against zero would call every chord a success.
        self.baseline = len(self.transfers)
        if self.baseline:
            print(f"    baseline: {self.baseline} eager transfer(s) before the chord")
        # Send the chord here rather than via inj.send_chord so the gap between
        # key events can be varied without rebuilding the Flatpak. Each event is
        # scheduled on the main loop instead of being pushed out back-to-back
        # with call_sync, so the loop stays free to serve SelectionTransfer
        # while the chord is in flight.
        from portals.inject import CHORDS, PRESSED, RELEASED

        keys = CHORDS.get(self.chord)
        if keys is None:
            print(f"    unknown chord {self.chord!r}")
            return False
        *mods, final = keys
        events = ([(m, PRESSED) for m in mods]
                  + [(final, PRESSED), (final, RELEASED)]
                  + [(m, RELEASED) for m in reversed(mods)])

        self.sent_at = (time.monotonic() - self.t0) * 1000
        print(f"    chord start         +{self.sent_at:7.1f} ms  gap={self.gap_ms} ms")

        def step(i: int) -> bool:
            if i >= len(events):
                done = (time.monotonic() - self.t0) * 1000
                print(f"    chord done          +{done:7.1f} ms")
                GLib.timeout_add(CHORD_WAIT_MS, self._report)
                return False
            keysym, state = events[i]
            try:
                self.inj._key(keysym, state)
            except GLib.Error as exc:
                print(f"    key {keysym:#x} FAILED: {exc.message}")
            GLib.timeout_add(self.gap_ms, lambda: step(i + 1))
            return False

        step(0)
        return False

    def _report(self) -> bool:
        print()
        print(f"  transfers seen: {len(self.transfers)}")
        if self.mode == "idle":
            if self.transfers:
                print("  VERDICT: EAGER -- the compositor pulls without a paste.")
                print("           SelectionTransfer is not a paste receipt.")
                print("           The escalating-paste design does not work.")
            else:
                print("  VERDICT: LAZY -- nothing read the clipboard on its own.")
                print("           A transfer can be treated as a paste receipt.")
        else:
            extra = self.transfers[self.baseline:]
            if extra:
                lag = extra[0][0] - self.sent_at
                print(f"  VERDICT: {self.chord!r} PASTED -- a transfer arrived beyond the")
                print(f"           baseline, +{lag:.1f} ms after the chord was sent")
            else:
                print(f"  VERDICT: {self.chord!r} did NOT paste -- only the eager baseline")
                print("           transfer arrived, so nothing read the clipboard")
        self._restore()
        return False

    def _restore(self) -> None:
        if self.saved is not None:
            self.inj._own_selection(self.saved)
            print("  clipboard put back")
        GLib.timeout_add(400, self._quit)

    def _quit(self, *_a) -> bool:
        self.loop.quit()
        return False


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "idle").lower()
    chord = sys.argv[2] if len(sys.argv) > 2 else "paste-key"
    gap_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    if mode not in ("idle", "chord"):
        print(__doc__)
        return 2

    print(f"spike_paste_ladder: mode={mode}" + (f" chord={chord} gap={gap_ms}ms" if mode == "chord" else ""))
    loop = GLib.MainLoop()
    probe = Probe(loop, mode, chord, gap_ms)
    GLib.idle_add(lambda: (probe.run(), False)[1])
    GLib.timeout_add(60000, probe._quit)
    loop.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
