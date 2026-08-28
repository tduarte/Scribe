"""Global hotkey handling via the GlobalShortcuts portal.

Measured behaviour on GNOME 50 / mutter 50 (see HoldDetector for why this
matters): the portal spec says `Activated` fires on key-down and `Deactivated`
on key-up, but GNOME never emits `Deactivated` at all. Instead it forwards
keyboard auto-repeat as a stream of `Activated` signals:

    key down ──▶ Activated
                 (~500 ms auto-repeat delay, silence)
                 Activated, Activated, ...  every ~30 ms while held
    key up   ──▶ (nothing)

So a press-and-hold has to be reconstructed from the timing of that stream. A
quick tap produces exactly one `Activated` and nothing else.

If a compositor ever does send `Deactivated`, we use it and switch the timing
heuristic off, so this degrades to the correct behaviour automatically.
"""

from __future__ import annotations

import logging
from typing import Callable

from gi.repository import GLib

from .base import Portal, PortalError, new_token

log = logging.getLogger(__name__)

IFACE = "org.freedesktop.portal.GlobalShortcuts"

SHORTCUT_DICTATE = "dictate"

# Silence long enough to mean "released", once auto-repeat has demonstrably
# started. The observed repeat interval is 30-31 ms, so 120 ms is ~4 missed
# repeats: comfortably clear of jitter, still imperceptible to the user.
REPEAT_GAP_MS = 120

# Before any repeat has arrived we cannot tell a hold from a tap, because the
# auto-repeat delay is ~500 ms of silence. Wait past that delay before deciding
# the key came back up. This is also why a tap records for ~650 ms rather than
# stopping instantly -- harmless for dictation, and it doubles as a trailing
# buffer that catches the last syllable.
INITIAL_GAP_MS = 650


class HoldDetector:
    """Turns GNOME's `Activated` stream into clean press/release callbacks."""

    def __init__(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        self._on_press = on_press
        self._on_release = on_release
        self._timeout: int | None = None
        self._held = False
        self._saw_repeat = False
        self._last_activated: float = 0.0
        # Purely informational. The timing fallback is NEVER disabled: a
        # compositor that sends Deactivated only sometimes would otherwise leave
        # the microphone open until the safety watchdog fired.
        self.compositor_sends_deactivated = False

    @property
    def held(self) -> bool:
        return self._held

    def activated(self) -> None:
        now = GLib.get_monotonic_time() / 1000.0
        gap = now - self._last_activated if self._last_activated else 0.0
        self._last_activated = now

        if not self._held:
            self._held = True
            self._saw_repeat = False
            log.debug("hold: press")
            self._on_press()
        else:
            # A second event while held means auto-repeat is running, so the
            # release timeout can be tightened dramatically.
            if not self._saw_repeat:
                log.debug("hold: auto-repeat detected after %.0f ms", gap)
            self._saw_repeat = True

        self._arm(REPEAT_GAP_MS if self._saw_repeat else INITIAL_GAP_MS)

    def deactivated(self) -> None:
        """A real key-up from the compositor: authoritative, so release now."""
        if not self.compositor_sends_deactivated:
            log.info("compositor emitted Deactivated")
            self.compositor_sends_deactivated = True
        self._disarm()
        self._release("deactivated")

    def cancel(self) -> None:
        """Abandon the current hold without firing the release callback."""
        self._disarm()
        self._held = False
        self._saw_repeat = False

    # -- internals -------------------------------------------------------

    def _arm(self, delay_ms: int) -> None:
        self._disarm()
        self._timeout = GLib.timeout_add(delay_ms, self._on_timeout)

    def _disarm(self) -> None:
        if self._timeout is not None:
            GLib.source_remove(self._timeout)
            self._timeout = None

    def _on_timeout(self) -> bool:
        self._timeout = None
        self._release("timeout")
        return GLib.SOURCE_REMOVE

    def _release(self, why: str) -> None:
        if not self._held:
            return
        self._held = False
        self._saw_repeat = False
        log.debug("hold: release (%s)", why)
        self._on_release()


class ShortcutManager:
    """Owns the GlobalShortcuts session and reports dictation press/release."""

    def __init__(
        self,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_error: Callable[[PortalError], None],
        *,
        preferred_trigger: str = "CTRL+ALT+space",
    ) -> None:
        self.portal = Portal()
        self.preferred_trigger = preferred_trigger
        self.session: str | None = None
        self.triggers: dict[str, str] = {}
        self._on_error = on_error
        self._subs: list[int] = []
        self.detector = HoldDetector(on_press, on_release)
        self.on_triggers_changed: Callable[[], None] | None = None

    def start(self) -> None:
        self._subs = [
            self.portal.subscribe_signal(IFACE, "Activated", self._activated),
            self.portal.subscribe_signal(IFACE, "Deactivated", self._deactivated),
            self.portal.subscribe_signal(IFACE, "ShortcutsChanged", self._changed),
        ]
        self.portal.request_call(
            IFACE, "CreateSession",
            lambda token: GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", token),
                "session_handle_token": GLib.Variant("s", new_token("scribe_sc")),
            },)),
            self._created,
        )

    def stop(self) -> None:
        self.detector.cancel()
        for sub in self._subs:
            self.portal.unsubscribe(sub)
        self._subs.clear()
        if self.session:
            self.portal.close_session(self.session)
            self.session = None

    def configure(self, parent_window: str = "") -> None:
        """Open GNOME's shortcut editor for our existing session.

        BindShortcuts may only be called once per session, so rebinding has to go
        through here rather than by tearing the session down.
        """
        if not self.session:
            return
        try:
            self.portal.call_noreply(
                IFACE, "ConfigureShortcuts",
                GLib.Variant("(osa{sv})", (self.session, parent_window, {})),
            )
        except GLib.Error as exc:
            log.warning("ConfigureShortcuts unavailable: %s", exc.message)

    # -- portal plumbing -------------------------------------------------

    def _created(self, results, error) -> None:
        if error:
            self._on_error(error)
            return
        self.session = results["session_handle"]
        # BindShortcuts must be called once per session -- ListShortcuts reports
        # only what the *current* session has bound, so it is always empty here
        # and cannot be used to skip this. GNOME does not re-prompt for
        # shortcuts the user has already confirmed for this app ID; it prompts
        # only for ones it has never seen, which is why adding a new shortcut id
        # in a later release will ask the user once.
        self._bind()

    def _bind(self) -> None:
        # Only one shortcut is registered. Releasing the key already ends
        # dictation, so a separate cancel binding earns nothing and costs the
        # user an extra confirmation dialog; cancelling is available as an
        # in-app action instead.
        shortcuts = [
            (SHORTCUT_DICTATE, {
                "description": GLib.Variant("s", "Hold to dictate"),
                "preferred_trigger": GLib.Variant("s", self.preferred_trigger),
            }),
        ]
        self.portal.request_call(
            IFACE, "BindShortcuts",
            lambda token: GLib.Variant(
                "(oa(sa{sv})sa{sv})",
                (self.session, shortcuts, "",
                 {"handle_token": GLib.Variant("s", token)}),
            ),
            self._bound,
        )

    def _bound(self, results, error) -> None:
        if error:
            self._on_error(error)
            return
        self._record_triggers(results.get("shortcuts", []))

    def _record_triggers(self, shortcuts) -> None:
        self.triggers = {
            sid: meta.get("trigger_description", "")
            for sid, meta in shortcuts
        }
        log.info("bound shortcuts: %s", self.triggers)
        if self.on_triggers_changed:
            self.on_triggers_changed()

    def _activated(self, session, shortcut_id, timestamp, options) -> None:
        if shortcut_id == SHORTCUT_DICTATE:
            self.detector.activated()

    def _deactivated(self, session, shortcut_id, timestamp, options) -> None:
        if shortcut_id == SHORTCUT_DICTATE:
            self.detector.deactivated()

    def _changed(self, session, shortcuts) -> None:
        self._record_triggers(shortcuts)
