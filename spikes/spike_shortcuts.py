#!/usr/bin/env python3
"""Phase 0 spike (a): does the GlobalShortcuts portal give us push-to-talk?

We need `Activated` on key-down and `Deactivated` on key-up, with usable
timestamps, so that holding a key can bracket a recording. Run this, approve the
shortcut when GNOME asks, then hold and release the key a few times.

    flatpak run --command=spike-shortcuts io.github.tduarte.Scribe
"""

from __future__ import annotations

import sys
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

sys.path.insert(0, "/app/share/scribe")
from portals.base import Portal, PortalError, new_token  # noqa: E402

IFACE = "org.freedesktop.portal.GlobalShortcuts"
SHORTCUT_ID = "dictate"
PREFERRED = "CTRL+ALT+space"

held_since: dict[str, float] = {}
presses = 0


def main() -> int:
    loop = GLib.MainLoop()
    portal = Portal()
    state: dict[str, str] = {}

    def on_activated(session, shortcut_id, timestamp, options):
        global presses
        presses += 1
        held_since[shortcut_id] = time.monotonic()
        print(f"  ACTIVATED   id={shortcut_id!r} portal_timestamp={timestamp}")

    def on_deactivated(session, shortcut_id, timestamp, options):
        start = held_since.pop(shortcut_id, None)
        held = f"{(time.monotonic() - start) * 1000:.0f} ms" if start else "?"
        print(f"  DEACTIVATED id={shortcut_id!r} portal_timestamp={timestamp}  held for {held}")
        if start:
            print("  -> press/release bracketing works; this is a usable push-to-talk primitive")

    def on_changed(session, shortcuts):
        print(f"  SHORTCUTS CHANGED: {shortcuts}")

    portal.subscribe_signal(IFACE, "Activated", on_activated)
    portal.subscribe_signal(IFACE, "Deactivated", on_deactivated)
    portal.subscribe_signal(IFACE, "ShortcutsChanged", on_changed)

    def bound(results, error):
        if error:
            print(f"FAIL  BindShortcuts: {error}")
            if error.cancelled:
                print("      You dismissed the dialog. The refusal is remembered; reset with:")
                print("      flatpak permission-reset io.github.tduarte.Scribe")
            loop.quit()
            return
        print("OK    BindShortcuts returned:")
        for sid, meta in results.get("shortcuts", []):
            desc = meta.get("description", "")
            trig = meta.get("trigger_description", "(none reported)")
            print(f"        {sid!r}: {desc!r}  trigger={trig!r}")
        print()
        print(f"Now hold {PREFERRED} (or whatever you bound) for ~1s and release. Ctrl+C to stop.")

    def created(results, error):
        if error:
            print(f"FAIL  CreateSession: {error}")
            loop.quit()
            return
        state["session"] = results["session_handle"]
        print(f"OK    session = {state['session']}")

        shortcuts = [(
            SHORTCUT_ID,
            {
                "description": GLib.Variant("s", "Hold to dictate"),
                "preferred_trigger": GLib.Variant("s", PREFERRED),
            },
        )]
        portal.request_call(
            IFACE, "BindShortcuts",
            lambda token: GLib.Variant(
                "(oa(sa{sv})sa{sv})",
                (state["session"], shortcuts, "",
                 {"handle_token": GLib.Variant("s", token)}),
            ),
            bound,
        )

    print(f"sender={portal.sender}")
    portal.request_call(
        IFACE, "CreateSession",
        lambda token: GLib.Variant("(a{sv})", ({
            "handle_token": GLib.Variant("s", token),
            "session_handle_token": GLib.Variant("s", new_token("scribe_sess")),
        },)),
        created,
    )

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    print(f"\n{presses} activation(s) seen.")
    if state.get("session"):
        portal.close_session(state["session"])
    return 0 if presses else 1


if __name__ == "__main__":
    sys.exit(main())
