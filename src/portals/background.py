"""Run in the background and appear in GNOME's Background Apps menu.

GNOME has no system tray and Flathub rejects tray-only apps, so the sanctioned
equivalent of Handy's tray icon is Background.SetStatus: it puts Scribe in
Quick Settings -> Background Apps with a live one-line status, and lets the user
quit us from there.
"""

from __future__ import annotations

import logging
from typing import Callable

from gi.repository import GLib

from .base import Portal, PortalError

log = logging.getLogger(__name__)

IFACE = "org.freedesktop.portal.Background"

# GNOME truncates the status past roughly this; keep it short and single-line.
STATUS_MAX = 96


class BackgroundManager:
    def __init__(self, portal: Portal | None = None) -> None:
        self.portal = portal or Portal()
        self._last_status: str | None = None

    def request(
        self,
        *,
        reason: str,
        autostart: bool,
        commandline: list[str],
        parent_window: str = "",
        callback: Callable[[bool, bool, PortalError | None], None] | None = None,
    ) -> None:
        """Ask to run in the background, optionally starting at login.

        The portal writes the host autostart file itself, so we need no
        filesystem permission for ~/.config/autostart.
        """
        def done(results, error):
            if error:
                log.warning("RequestBackground: %s", error)
                if callback:
                    callback(False, False, error)
                return
            if callback:
                callback(
                    bool(results.get("background", False)),
                    bool(results.get("autostart", False)),
                    None,
                )

        self.portal.request_call(
            IFACE, "RequestBackground",
            lambda token: GLib.Variant("(sa{sv})", (parent_window, {
                "handle_token": GLib.Variant("s", token),
                "reason": GLib.Variant("s", reason),
                "autostart": GLib.Variant("b", autostart),
                "commandline": GLib.Variant("as", commandline),
                "dbus-activatable": GLib.Variant("b", False),
            })),
            done,
        )

    def set_status(self, message: str) -> None:
        """Update the line shown under Background Apps."""
        text = message.strip()[:STATUS_MAX]
        if text == self._last_status:
            return
        try:
            self.portal.call_noreply(
                IFACE, "SetStatus",
                GLib.Variant("(a{sv})", ({"message": GLib.Variant("s", text)},)),
            )
            self._last_status = text
        except GLib.Error as exc:
            log.debug("SetStatus: %s", exc.message)
