"""Desktop notifications via the portal.

Used sparingly and deliberately: errors, and finishing a model download. A
notification per dictation would be noise, and the audio cue already covers it.
"""

from __future__ import annotations

import logging

from gi.repository import GLib

from .base import Portal

log = logging.getLogger(__name__)

IFACE = "org.freedesktop.portal.Notification"


class Notifier:
    def __init__(self, portal: Portal | None = None) -> None:
        self.portal = portal or Portal()

    def notify(
        self,
        notification_id: str,
        title: str,
        body: str = "",
        *,
        priority: str = "normal",
        icon: str | None = None,
    ) -> None:
        payload = {
            "title": GLib.Variant("s", title),
            "body": GLib.Variant("s", body),
            "priority": GLib.Variant("s", priority),
        }
        if icon:
            payload["icon"] = GLib.Variant("(sv)", ("themed", GLib.Variant("as", [icon])))
        try:
            self.portal.call_noreply(
                IFACE, "AddNotification",
                GLib.Variant("(sa{sv})", (notification_id, payload)),
            )
        except GLib.Error as exc:
            log.debug("AddNotification: %s", exc.message)

    def withdraw(self, notification_id: str) -> None:
        try:
            self.portal.call_noreply(
                IFACE, "RemoveNotification", GLib.Variant("(s)", (notification_id,))
            )
        except GLib.Error as exc:
            log.debug("RemoveNotification: %s", exc.message)
