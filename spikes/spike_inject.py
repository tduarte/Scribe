#!/usr/bin/env python3
"""Phase 0 spike (b/c/d): can we put text into the focused window?

Exercises the three things the plan depends on and cannot verify by reading docs:

  (b) does a persist_mode=2 session skip the consent dialog on a later run?
  (c) does clipboard(portal) + synthetic Ctrl+V land text in the focused app?
  (d) does mutter accept Unicode keysyms (0x01000000 + codepoint)?

    flatpak run --command=spike-inject io.github.tduarte.Scribe [paste|type|both]

Focus a text editor during the countdown. The token is cached, so run it twice
(and once after a reboot) to answer (b).
"""

from __future__ import annotations

import json
import os
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib  # noqa: E402

sys.path.insert(0, "/app/share/scribe/scribe")
from portals.base import PORTAL_BUS, PORTAL_PATH, Portal, new_token  # noqa: E402

RD = "org.freedesktop.portal.RemoteDesktop"
CB = "org.freedesktop.portal.Clipboard"

DEVICE_KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2
PRESSED, RELEASED = 1, 0

KEY_CONTROL_L = 0xFFE3
KEY_SHIFT_L = 0xFFE1
KEY_V = 0x0076

PASTE_TEXT = "Scribe paste test: café naïve — \U0001f399️"
TYPE_TEXT = "Scribe type test: café — \U0001f399"

TOKEN_FILE = os.path.join(
    GLib.get_user_config_dir(), "scribe-spike-restore-token.json"
)

mode = (sys.argv[1] if len(sys.argv) > 1 else "both").lower()


def load_token() -> str | None:
    try:
        with open(TOKEN_FILE) as fh:
            return json.load(fh).get("restore_token")
    except (OSError, ValueError):
        return None


def save_token(token: str | None) -> None:
    if not token:
        return
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as fh:
        json.dump({"restore_token": token}, fh)
    print(f"      cached restore_token -> {TOKEN_FILE}")


class Injector:
    def __init__(self, loop: GLib.MainLoop) -> None:
        self.loop = loop
        self.portal = Portal()
        self.session: str | None = None
        self.clipboard_ok = False
        self.pending_text = PASTE_TEXT
        self.results: dict[str, str] = {}

    # -- session ---------------------------------------------------------

    def start(self) -> None:
        self.portal.bus.signal_subscribe(
            PORTAL_BUS, CB, "SelectionTransfer", PORTAL_PATH, None,
            Gio.DBusSignalFlags.NONE,
            lambda _c, _s, _p, _i, _sig, params: self.on_selection_transfer(*params.unpack()),
        )
        self.portal.request_call(
            RD, "CreateSession",
            lambda token: GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", token),
                "session_handle_token": GLib.Variant("s", new_token("scribe_rd")),
            },)),
            self.on_created,
        )

    def on_created(self, results, error) -> None:
        if error:
            print(f"FAIL  CreateSession: {error}")
            self.loop.quit()
            return
        self.session = results["session_handle"]
        print(f"OK    session = {self.session}")

        opts = {
            "handle_token": None,  # filled per-call
            "types": GLib.Variant("u", DEVICE_KEYBOARD),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        cached = load_token()
        if cached:
            opts["restore_token"] = GLib.Variant("s", cached)
            print("      reusing cached restore_token -- if no dialog appears, (b) PASSES")
        else:
            print("      no cached token; expect a consent dialog this run")

        def build(token):
            o = {k: v for k, v in opts.items() if v is not None}
            o["handle_token"] = GLib.Variant("s", token)
            return GLib.Variant("(oa{sv})", (self.session, o))

        self.portal.request_call(RD, "SelectDevices", build, self.on_devices)

    def on_devices(self, results, error) -> None:
        if error:
            print(f"FAIL  SelectDevices: {error}")
            self.loop.quit()
            return
        print("OK    SelectDevices")
        # Clipboard must be requested BEFORE Start.
        try:
            self.portal.bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, CB, "RequestClipboard",
                GLib.Variant("(oa{sv})", (self.session, {})), None,
                Gio.DBusCallFlags.NONE, -1, None,
            )
            print("OK    RequestClipboard")
        except GLib.Error as exc:
            print(f"WARN  RequestClipboard failed: {exc.message}")

        self.portal.request_call(
            RD, "Start",
            lambda token: GLib.Variant(
                "(osa{sv})",
                (self.session, "", {"handle_token": GLib.Variant("s", token)}),
            ),
            self.on_started,
        )

    def on_started(self, results, error) -> None:
        if error:
            print(f"FAIL  Start: {error}")
            self.loop.quit()
            return
        devices = results.get("devices", 0)
        self.clipboard_ok = bool(results.get("clipboard_enabled", False))
        print(f"OK    Start: devices=0b{devices:b} clipboard_enabled={self.clipboard_ok}")
        if not devices & DEVICE_KEYBOARD:
            print("FAIL  no keyboard device granted -- injection impossible")
            self.loop.quit()
            return
        save_token(results.get("restore_token"))

        self.countdown = 10
        print("\n>>> Focus a text editor NOW.")
        GLib.timeout_add(1000, self._tick)

    def _tick(self) -> bool:
        if self.countdown > 0:
            print(f"    {self.countdown}...", flush=True)
            self.countdown -= 1
            return GLib.SOURCE_CONTINUE
        self.run_tests()
        return GLib.SOURCE_REMOVE

    # -- key synthesis ---------------------------------------------------

    def key(self, keysym: int, state: int) -> None:
        self.portal.bus.call_sync(
            PORTAL_BUS, PORTAL_PATH, RD, "NotifyKeyboardKeysym",
            GLib.Variant("(oa{sv}iu)", (self.session, {}, keysym, state)),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )

    # -- tests -----------------------------------------------------------

    def run_tests(self) -> None:
        if mode in ("paste", "both"):
            self.test_paste()
            GLib.timeout_add(2500, self._after_paste)
        else:
            self._after_paste()

    def _after_paste(self) -> bool:
        if mode in ("type", "both"):
            self.test_type()
        self.report()
        self.loop.quit()
        return GLib.SOURCE_REMOVE

    def test_paste(self) -> None:
        print("\n--- (c) clipboard + Ctrl+V ---")
        if not self.clipboard_ok:
            self.results["paste"] = "SKIP (clipboard not enabled)"
            print("SKIP  clipboard access was not granted")
            return
        self.pending_text = PASTE_TEXT
        try:
            self.portal.bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, CB, "SetSelection",
                GLib.Variant("(oa{sv})", (self.session, {
                    "mime_types": GLib.Variant("as", ["text/plain;charset=utf-8", "text/plain"]),
                })),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            print("OK    SetSelection (we now own the clipboard)")
        except GLib.Error as exc:
            self.results["paste"] = f"FAIL SetSelection: {exc.message}"
            print(f"FAIL  SetSelection: {exc.message}")
            return
        # Give the compositor a beat to register the new selection owner, then
        # send the chord from a timeout so the loop can serve SelectionTransfer.
        GLib.timeout_add(400, self._send_paste_chord)

    def _send_paste_chord(self) -> bool:
        self.key(KEY_CONTROL_L, PRESSED)
        self.key(KEY_V, PRESSED)
        self.key(KEY_V, RELEASED)
        self.key(KEY_CONTROL_L, RELEASED)
        self.results["paste"] = "sent -- check your editor"
        print(f"      sent Ctrl+V; expected text: {PASTE_TEXT!r}")
        return GLib.SOURCE_REMOVE

    def test_type(self) -> None:
        print("\n--- (d) Unicode keysym typing ---")
        sent = 0
        try:
            for ch in TYPE_TEXT:
                cp = ord(ch)
                keysym = cp if cp < 0x80 else 0x01000000 + cp
                self.key(keysym, PRESSED)
                self.key(keysym, RELEASED)
                sent += 1
        except GLib.Error as exc:
            self.results["type"] = f"FAIL after {sent} chars: {exc.message}"
            print(f"FAIL  after {sent} chars: {exc.message}")
            return
        self.results["type"] = f"sent {sent} chars -- check your editor"
        print(f"      sent {sent} chars; expected: {TYPE_TEXT!r}")
        print("      NB: D-Bus accepting the call only proves it did not error;")
        print("          confirm visually that non-ASCII actually appeared.")

    def on_selection_transfer(self, session, mime_type, serial) -> None:
        """The compositor is asking us for the clipboard contents."""
        print(f"      SelectionTransfer mime={mime_type!r} serial={serial}")
        try:
            reply, fds = self.portal.bus.call_with_unix_fd_list_sync(
                PORTAL_BUS, PORTAL_PATH, CB, "SelectionWrite",
                GLib.Variant("(ou)", (self.session, serial)),
                GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, -1, None, None,
            )
            fd = fds.get(reply.unpack()[0])
            os.write(fd, self.pending_text.encode("utf-8"))
            os.close(fd)
            self.portal.bus.call_sync(
                PORTAL_BUS, PORTAL_PATH, CB, "SelectionWriteDone",
                GLib.Variant("(oub)", (self.session, serial, True)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )
            print("      served clipboard contents OK")
        except (GLib.Error, OSError) as exc:
            print(f"FAIL  serving clipboard: {exc}")

    def report(self) -> None:
        print("\n=== RESULTS ===")
        for k, v in self.results.items():
            print(f"  {k:6s}: {v}")
        print("  (b) persist: re-run this command; a second run with NO dialog means PASS")


def main() -> int:
    loop = GLib.MainLoop()
    inj = Injector(loop)
    inj.start()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    if inj.session:
        inj.portal.close_session(inj.session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
