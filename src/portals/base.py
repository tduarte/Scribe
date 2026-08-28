"""Thin async client for the XDG desktop portals.

The GNOME 50 runtime ships no libportal, so we talk to
``org.freedesktop.portal.Desktop`` over D-Bus directly. Everything here stays on
the GLib main loop -- there are no worker threads anywhere in Scribe.

The portal request pattern is: call a method, which immediately returns an
object path for an ``org.freedesktop.portal.Request``; the real answer arrives
later as a ``Response`` signal on that path. We subscribe *before* issuing the
call so a fast reply cannot race us.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Callable

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"

# Response codes from the Request interface.
RESPONSE_OK = 0
RESPONSE_CANCELLED = 1
RESPONSE_failed = 2  # spelled per spec: "ended in some other way"


class PortalError(Exception):
    """A portal call failed, was cancelled, or was denied by the user."""

    def __init__(self, message: str, *, cancelled: bool = False) -> None:
        super().__init__(message)
        self.cancelled = cancelled


def new_token(prefix: str) -> str:
    """A unique token for a request or session handle."""
    return f"{prefix}_{secrets.token_hex(8)}"


class Portal:
    """Shared connection and handle-path bookkeeping for the portal APIs."""

    def __init__(self, connection: Gio.DBusConnection | None = None) -> None:
        self.bus = connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
        unique = self.bus.get_unique_name()  # ":1.234"
        # The portal derives request/session paths from the caller's unique name
        # with the leading colon dropped and dots turned into underscores.
        self.sender = unique[1:].replace(".", "_")

    # -- path helpers ----------------------------------------------------

    def request_path(self, token: str) -> str:
        return f"{PORTAL_PATH}/request/{self.sender}/{token}"

    def session_path(self, token: str) -> str:
        return f"{PORTAL_PATH}/session/{self.sender}/{token}"

    # -- calls -----------------------------------------------------------

    def request_call(
        self,
        interface: str,
        method: str,
        build_args: Callable[[str], GLib.Variant],
        callback: Callable[[dict[str, Any] | None, PortalError | None], None],
    ) -> None:
        """Issue a Request-returning portal call.

        ``build_args`` receives the generated ``handle_token`` and must return the
        full argument tuple including an options dict carrying that token.
        """
        token = new_token("scribe")
        expected = self.request_path(token)
        state: dict[str, Any] = {"done": False, "subs": []}

        def finish(results: dict[str, Any] | None, error: PortalError | None) -> None:
            if state["done"]:
                return
            state["done"] = True
            for sub in state["subs"]:
                self.bus.signal_unsubscribe(sub)
            state["subs"].clear()
            callback(results, error)

        def on_response(_conn, _sender, _path, _iface, _signal, params) -> None:
            code = params.unpack()[0]
            results = params.unpack()[1]
            if code == RESPONSE_OK:
                finish(results, None)
            elif code == RESPONSE_CANCELLED:
                finish(None, PortalError(f"{method}: cancelled by the user", cancelled=True))
            else:
                finish(None, PortalError(f"{method}: the portal ended the request (code {code})"))

        def subscribe(path: str) -> None:
            state["subs"].append(
                self.bus.signal_subscribe(
                    PORTAL_BUS, REQUEST_IFACE, "Response", path, None,
                    Gio.DBusSignalFlags.NONE, on_response,
                )
            )

        # Subscribe first so a fast reply cannot arrive before we are listening.
        subscribe(expected)

        def on_call_done(bus: Gio.DBusConnection, res: Gio.AsyncResult) -> None:
            try:
                reply = bus.call_finish(res)
            except GLib.Error as exc:
                finish(None, PortalError(f"{method}: {exc.message}"))
                return
            actual = reply.unpack()[0]
            if actual != expected:
                # Older portal implementations may not use the predicted path.
                log.debug("%s: request path %s != predicted %s", method, actual, expected)
                subscribe(actual)

        self.bus.call(
            PORTAL_BUS, PORTAL_PATH, interface, method, build_args(token),
            GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE, -1, None,
            on_call_done,
        )

    def call_noreply(
        self,
        interface: str,
        method: str,
        args: GLib.Variant,
        reply_type: str | None = None,
    ) -> GLib.Variant:
        """Call a portal method that answers directly, with no Request round-trip."""
        vt = GLib.VariantType.new(reply_type) if reply_type else None
        return self.bus.call_sync(
            PORTAL_BUS, PORTAL_PATH, interface, method, args, vt,
            Gio.DBusCallFlags.NONE, -1, None,
        )

    def subscribe_signal(
        self, interface: str, signal: str, handler: Callable[..., None]
    ) -> int:
        return self.bus.signal_subscribe(
            PORTAL_BUS, interface, signal, PORTAL_PATH, None,
            Gio.DBusSignalFlags.NONE,
            lambda _c, _s, _p, _i, _sig, params: handler(*params.unpack()),
        )

    def unsubscribe(self, subscription_id: int) -> None:
        self.bus.signal_unsubscribe(subscription_id)

    def close_session(self, session_handle: str) -> None:
        try:
            self.bus.call_sync(
                PORTAL_BUS, session_handle, SESSION_IFACE, "Close", None, None,
                Gio.DBusCallFlags.NONE, -1, None,
            )
        except GLib.Error as exc:
            log.debug("closing session %s: %s", session_handle, exc.message)
