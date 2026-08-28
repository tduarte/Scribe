"""Deliver transcribed text into whatever application currently has focus.

On GNOME/Wayland this is the only route that works from a sandbox: mutter
implements no virtual-keyboard protocol, and /dev/uinput (ydotool) is off limits
to a Flatpak. So we hold one long-lived RemoteDesktop keyboard session, take
ownership of the clipboard through the Clipboard portal, and synthesize a paste
chord.

Two things learned the hard way (docs/PORTAL-FINDINGS.md):

* Per-character typing via NotifyKeyboardKeysym is NOT usable. mutter resolves
  keysyms against the active keyboard layout and silently discards anything not
  on it, so accented characters vanish mid-word with no error. Pasting carries
  arbitrary Unicode intact, so pasting is what we do.
* The compositor may ask for the clipboard payload more than once per paste, and
  it asks on the main loop. Nothing in this path may block.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from typing import Callable

from gi.repository import Gio, GLib

from .base import PORTAL_BUS, PORTAL_PATH, Portal, PortalError, new_token

log = logging.getLogger(__name__)

RD = "org.freedesktop.portal.RemoteDesktop"
CB = "org.freedesktop.portal.Clipboard"

DEVICE_KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2
PRESSED, RELEASED = 1, 0

KEY_CONTROL_L = 0xFFE3
KEY_SHIFT_L = 0xFFE1
KEY_V = 0x0076
KEY_INSERT = 0xFF63

TEXT_MIME = "text/plain;charset=utf-8"
MIME_TYPES = [TEXT_MIME, "text/plain", "UTF8_STRING"]

CHORDS: dict[str, tuple[int, ...]] = {
    "ctrl-v": (KEY_CONTROL_L, KEY_V),
    "ctrl-shift-v": (KEY_CONTROL_L, KEY_SHIFT_L, KEY_V),
    "shift-insert": (KEY_SHIFT_L, KEY_INSERT),
}


class InjectorState(Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    READY = "ready"
    UNAVAILABLE = "unavailable"


class TextInjector:
    """Owns the RemoteDesktop + Clipboard session used to paste text."""

    def __init__(
        self,
        *,
        get_token: Callable[[], str],
        set_token: Callable[[str], None],
        on_state_change: Callable[[InjectorState, str], None] | None = None,
    ) -> None:
        self.portal = Portal()
        self._get_token = get_token
        self._set_token = set_token
        self._on_state_change = on_state_change

        self.state = InjectorState.IDLE
        self.session: str | None = None
        self.clipboard_enabled = False

        # Served to the compositor on demand; kept until the selection changes,
        # because a single paste can trigger several SelectionTransfer calls.
        self._payload: bytes = b""
        self._saved: bytes | None = None
        self._waiters: list[Callable[[bool], None]] = []

        self.portal.bus.signal_subscribe(
            PORTAL_BUS, CB, "SelectionTransfer", PORTAL_PATH, None,
            Gio.DBusSignalFlags.NONE,
            lambda _c, _s, _p, _i, _sg, params: self._on_transfer(*params.unpack()),
        )

    # -- session ---------------------------------------------------------

    def _set_state(self, state: InjectorState, detail: str = "") -> None:
        self.state = state
        log.info("injector state: %s %s", state.value, detail)
        if self._on_state_change:
            self._on_state_change(state, detail)

    def ensure_session(self, callback: Callable[[bool], None] | None = None) -> None:
        """Create the session if needed; callback(ok) when ready or failed."""
        if self.state is InjectorState.READY:
            if callback:
                callback(True)
            return
        if callback:
            self._waiters.append(callback)
        if self.state is InjectorState.CONNECTING:
            return

        self._set_state(InjectorState.CONNECTING)
        self.portal.request_call(
            RD, "CreateSession",
            lambda token: GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", token),
                "session_handle_token": GLib.Variant("s", new_token("scribe_rd")),
            },)),
            self._on_created,
        )

    def _settle(self, ok: bool) -> None:
        waiters, self._waiters = self._waiters, []
        for cb in waiters:
            cb(ok)

    def _fail(self, why: str) -> None:
        self._set_state(InjectorState.UNAVAILABLE, why)
        self._settle(False)

    def _on_created(self, results, error) -> None:
        if error:
            self._fail(str(error))
            return
        self.session = results["session_handle"]

        opts = {
            "types": GLib.Variant("u", DEVICE_KEYBOARD),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        token = self._get_token()
        if token:
            opts["restore_token"] = GLib.Variant("s", token)

        def build(handle):
            o = dict(opts)
            o["handle_token"] = GLib.Variant("s", handle)
            return GLib.Variant("(oa{sv})", (self.session, o))

        self.portal.request_call(RD, "SelectDevices", build, self._on_devices)

    def _on_devices(self, results, error) -> None:
        if error:
            self._fail(str(error))
            return
        # Clipboard integration has no session of its own; it must be requested
        # on this session BEFORE Start().
        try:
            self.portal.call_noreply(
                CB, "RequestClipboard",
                GLib.Variant("(oa{sv})", (self.session, {})),
            )
        except GLib.Error as exc:
            log.warning("RequestClipboard failed: %s", exc.message)

        self.portal.request_call(
            RD, "Start",
            lambda token: GLib.Variant(
                "(osa{sv})",
                (self.session, "", {"handle_token": GLib.Variant("s", token)}),
            ),
            self._on_started,
        )

    def _on_started(self, results, error) -> None:
        if error:
            self._fail(str(error))
            return
        devices = results.get("devices", 0)
        self.clipboard_enabled = bool(results.get("clipboard_enabled", False))
        # The token is reissued on every Start; always overwrite the stored one.
        if results.get("restore_token"):
            self._set_token(results["restore_token"])
        if not devices & DEVICE_KEYBOARD:
            self._fail("no keyboard device was granted")
            return
        self._set_state(InjectorState.READY)
        self._settle(True)

    def close(self) -> None:
        if self.session:
            self.portal.close_session(self.session)
            self.session = None
        self._set_state(InjectorState.IDLE)

    # -- key synthesis ---------------------------------------------------

    def _key(self, keysym: int, state: int) -> None:
        self.portal.bus.call_sync(
            PORTAL_BUS, PORTAL_PATH, RD, "NotifyKeyboardKeysym",
            GLib.Variant("(oa{sv}iu)", (self.session, {}, keysym, state)),
            None, Gio.DBusCallFlags.NONE, -1, None,
        )

    def send_chord(self, chord: str) -> None:
        """Press modifiers, tap the final key, release in reverse order."""
        keys = CHORDS.get(chord, CHORDS["ctrl-v"])
        *mods, final = keys
        for m in mods:
            self._key(m, PRESSED)
        self._key(final, PRESSED)
        self._key(final, RELEASED)
        for m in reversed(mods):
            self._key(m, RELEASED)

    # -- clipboard -------------------------------------------------------

    def _on_transfer(self, session, mime_type, serial) -> None:
        if session != self.session:
            return
        try:
            reply, fds = self.portal.bus.call_with_unix_fd_list_sync(
                PORTAL_BUS, PORTAL_PATH, CB, "SelectionWrite",
                GLib.Variant("(ou)", (self.session, serial)),
                GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, -1, None, None,
            )
            fd = fds.get(reply.unpack()[0])
            with os.fdopen(fd, "wb") as fh:
                fh.write(self._payload)
            ok = True
        except (GLib.Error, OSError) as exc:
            log.warning("serving clipboard: %s", exc)
            ok = False
        try:
            self.portal.call_noreply(
                CB, "SelectionWriteDone",
                GLib.Variant("(oub)", (self.session, serial, ok)),
            )
        except GLib.Error as exc:
            log.debug("SelectionWriteDone: %s", exc.message)

    def _own_selection(self, data: bytes) -> None:
        self._payload = data
        self.portal.call_noreply(
            CB, "SetSelection",
            GLib.Variant("(oa{sv})", (self.session, {
                "mime_types": GLib.Variant("as", MIME_TYPES),
            })),
        )

    def _read_selection(self, done: Callable[[bytes | None], None]) -> None:
        """Read the current clipboard so it can be put back after pasting."""
        try:
            reply, fds = self.portal.bus.call_with_unix_fd_list_sync(
                PORTAL_BUS, PORTAL_PATH, CB, "SelectionRead",
                GLib.Variant("(os)", (self.session, TEXT_MIME)),
                GLib.VariantType.new("(h)"), Gio.DBusCallFlags.NONE, -1, None, None,
            )
            fd = fds.get(reply.unpack()[0])
        except GLib.Error as exc:
            log.debug("SelectionRead unavailable: %s", exc.message)
            done(None)
            return

        stream = Gio.UnixInputStream.new(fd, True)
        chunks: list[bytes] = []

        def pump(src: Gio.InputStream, res: Gio.AsyncResult) -> None:
            try:
                data = src.read_bytes_finish(res).get_data()
            except GLib.Error as exc:
                log.debug("reading clipboard: %s", exc.message)
                done(None)
                return
            if data:
                chunks.append(data)
                src.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, pump)
            else:
                src.close(None)
                done(b"".join(chunks))

        stream.read_bytes_async(65536, GLib.PRIORITY_DEFAULT, None, pump)

    # -- public API ------------------------------------------------------

    def paste(
        self,
        text: str,
        *,
        chord: str = "ctrl-v",
        restore_clipboard: bool = True,
        delay_ms: int = 60,
        on_done: Callable[[bool, str], None] | None = None,
    ) -> None:
        """Put `text` on the clipboard and paste it into the focused window."""
        if not text:
            if on_done:
                on_done(True, "")
            return

        def proceed(ok: bool) -> None:
            if not ok or not self.clipboard_enabled:
                if on_done:
                    on_done(False, "clipboard access was not granted")
                return
            payload = text.encode("utf-8")

            def after_save(saved: bytes | None) -> None:
                self._saved = saved if restore_clipboard else None
                self._own_selection(payload)
                # Let the compositor register the new owner before the chord.
                GLib.timeout_add(max(delay_ms, 50), lambda: self._do_paste(chord, delay_ms, on_done))

            if restore_clipboard:
                self._read_selection(after_save)
            else:
                after_save(None)

        self.ensure_session(proceed)

    def _do_paste(self, chord: str, delay_ms: int, on_done) -> bool:
        try:
            self.send_chord(chord)
        except GLib.Error as exc:
            if on_done:
                on_done(False, exc.message)
            return GLib.SOURCE_REMOVE

        if self._saved is not None:
            saved = self._saved
            self._saved = None
            # Give the target app time to actually read the selection before we
            # hand ownership back to the previous contents.
            GLib.timeout_add(max(delay_ms * 4, 250), lambda: self._restore(saved))
        if on_done:
            on_done(True, "")
        return GLib.SOURCE_REMOVE

    def _restore(self, saved: bytes) -> bool:
        try:
            self._own_selection(saved)
        except GLib.Error as exc:
            log.debug("restoring clipboard: %s", exc.message)
        return GLib.SOURCE_REMOVE

    def copy_only(
        self, text: str, on_done: Callable[[bool, str], None] | None = None
    ) -> None:
        """Take the clipboard without pasting, for clipboard-only output mode."""
        def proceed(ok: bool) -> None:
            if not ok or not self.clipboard_enabled:
                if on_done:
                    on_done(False, "clipboard access was not granted")
                return
            self._own_selection(text.encode("utf-8"))
            if on_done:
                on_done(True, "")

        self.ensure_session(proceed)
