#!/usr/bin/env python3
"""Measure how GlobalShortcuts actually delivers a press-and-hold.

Reports every Activated with the gap since the previous one, and the press and
release that HoldDetector infers from them, so a mis-timed release is visible as
a number rather than a feeling.

    flatpak run --command=spike-shortcuts io.github.tduarte.Scribe
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

sys.path.insert(0, "/app/share/scribe/scribe")
from portals.base import Portal, new_token  # noqa: E402
from portals.shortcuts import (INITIAL_GAP_MS, REPEAT_GAP_MS,  # noqa: E402
                               HoldDetector)

IFACE = "org.freedesktop.portal.GlobalShortcuts"
DICTATE = "dictate"


def now_ms() -> float:
    return GLib.get_monotonic_time() / 1000.0


class Probe:
    def __init__(self) -> None:
        self.portal = Portal()
        self.session = None
        self.last = 0.0
        self.press_at = 0.0
        self.last_activated = 0.0
        self.holds = 0
        self.deactivated_seen = 0
        self.detector = HoldDetector(self.on_press, self.on_release)

    def on_press(self) -> None:
        self.press_at = now_ms()
        print("    >>> PRESS inferred")

    def on_release(self) -> None:
        t = now_ms()
        held = t - self.press_at
        lag = t - self.last_activated
        self.holds += 1
        print(f"    <<< RELEASE inferred: held {held:.0f} ms, "
              f"{lag:.0f} ms after the last Activated")
        print(f"        (a release lag near {REPEAT_GAP_MS} ms is good; near "
              f"{INITIAL_GAP_MS} ms means no auto-repeat arrived)\n")

    def activated(self, session, sid, ts, options) -> None:
        if sid != DICTATE:
            print(f"    [other shortcut fired: {sid!r}]")
            return
        t = now_ms()
        gap = t - self.last if self.last else 0.0
        self.last = t
        self.last_activated = t
        print(f"  Activated  gap={gap:8.0f} ms")
        self.detector.activated()

    def deactivated(self, session, sid, ts, options) -> None:
        self.deactivated_seen += 1
        print(f"  Deactivated (id={sid!r})  <-- compositor reported key-up")
        if sid == DICTATE:
            self.detector.deactivated()

    def start(self, loop) -> None:
        self.portal.subscribe_signal(IFACE, "Activated", self.activated)
        self.portal.subscribe_signal(IFACE, "Deactivated", self.deactivated)

        def bound(results, error):
            if error:
                print(f"FAIL BindShortcuts: {error}")
                loop.quit()
                return
            for sid, meta in results.get("shortcuts", []):
                print(f"  {sid}: {meta.get('trigger_description', '?')}")
            print("\nHold the dictate shortcut for ~2 s and release. Do it 3 times.\n")

        def created(results, error):
            if error:
                print(f"FAIL CreateSession: {error}")
                loop.quit()
                return
            self.session = results["session_handle"]
            # ListShortcuts only reports what *this* session bound, so it is
            # always empty here; we must bind to receive Activated at all.
            # GNOME does not re-prompt for shortcuts already confirmed for this
            # app ID, so this is silent once the app has been run.
            shortcuts = [
                ("dictate", {
                    "description": GLib.Variant("s", "Hold to dictate"),
                    "preferred_trigger": GLib.Variant("s", "CTRL+ALT+space"),
                }),
            ]
            self.portal.request_call(
                IFACE, "BindShortcuts",
                lambda tok: GLib.Variant("(oa(sa{sv})sa{sv})", (
                    self.session, shortcuts, "",
                    {"handle_token": GLib.Variant("s", tok)})),
                bound,
            )

        self.portal.request_call(
            IFACE, "CreateSession",
            lambda tok: GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", tok),
                "session_handle_token": GLib.Variant("s", new_token("probe")),
            },)),
            created,
        )


def main() -> int:
    loop = GLib.MainLoop()
    probe = Probe()
    probe.start(loop)
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    print(f"\n{probe.holds} hold(s), {probe.deactivated_seen} Deactivated signal(s)")
    if probe.session:
        probe.portal.close_session(probe.session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
