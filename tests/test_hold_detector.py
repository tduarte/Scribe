"""Replays the Activated timing actually observed on GNOME 50.

These run against a real GLib main loop, so they take real time -- but they are
the only way to prove the release heuristic behaves on the pattern that mutter
genuinely produces.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from gi.repository import GLib

from portals.shortcuts import INITIAL_GAP_MS, REPEAT_GAP_MS, HoldDetector

# Observed on this machine: one key-down event, ~500 ms of silence, then
# auto-repeat every 30-31 ms for as long as the key is held.
REPEAT_DELAY_MS = 500
REPEAT_INTERVAL_MS = 30


class Recorder:
    def __init__(self):
        self.events = []

    def press(self):
        self.events.append(("press", GLib.get_monotonic_time() // 1000))

    def release(self):
        self.events.append(("release", GLib.get_monotonic_time() // 1000))

    @property
    def kinds(self):
        return [e[0] for e in self.events]


def run_schedule(detector, offsets_ms, settle_ms):
    """Fire detector.activated() at each offset, then let the loop settle."""
    loop = GLib.MainLoop()
    for off in offsets_ms:
        GLib.timeout_add(off, lambda: (detector.activated(), False)[1])
    GLib.timeout_add(max(offsets_ms, default=0) + settle_ms, lambda: (loop.quit(), False)[1])
    loop.run()


def hold_schedule(duration_ms):
    """Offsets for a physical hold of the given duration."""
    offsets = [0]
    t = REPEAT_DELAY_MS
    while t <= duration_ms:
        offsets.append(t)
        t += REPEAT_INTERVAL_MS
    return offsets


def test_single_tap_is_one_press_and_one_release():
    rec = Recorder()
    det = HoldDetector(rec.press, rec.release)
    run_schedule(det, [0], INITIAL_GAP_MS + 200)
    assert rec.kinds == ["press", "release"]


def test_tap_release_waits_out_the_repeat_delay():
    """A lone Activated must not be called released before the repeat delay."""
    rec = Recorder()
    det = HoldDetector(rec.press, rec.release)
    run_schedule(det, [0], INITIAL_GAP_MS + 200)
    press_at, release_at = rec.events[0][1], rec.events[1][1]
    held = release_at - press_at
    assert held >= REPEAT_DELAY_MS, f"released after {held}ms, before repeats could arrive"


def test_one_second_hold_is_a_single_press_release_pair():
    rec = Recorder()
    det = HoldDetector(rec.press, rec.release)
    run_schedule(det, hold_schedule(1000), REPEAT_GAP_MS + 250)
    assert rec.kinds == ["press", "release"], f"got {rec.kinds}"


def test_long_hold_release_is_detected_promptly():
    """Once repeating, release should be noticed within ~REPEAT_GAP_MS."""
    rec = Recorder()
    det = HoldDetector(rec.press, rec.release)
    offsets = hold_schedule(1000)
    run_schedule(det, offsets, REPEAT_GAP_MS + 250)
    last_repeat = offsets[-1]
    release_at = rec.events[-1][1] - rec.events[0][1]
    lag = release_at - last_repeat
    assert lag < REPEAT_GAP_MS + 120, f"release lagged {lag}ms after the last repeat"


def test_two_consecutive_holds_do_not_merge():
    rec = Recorder()
    det = HoldDetector(rec.press, rec.release)
    first = hold_schedule(900)
    gap = first[-1] + 900          # observed inter-hold gaps were 700-2300 ms
    second = [gap + o for o in hold_schedule(900)]
    run_schedule(det, first + second, REPEAT_GAP_MS + 250)
    assert rec.kinds == ["press", "release", "press", "release"], f"got {rec.kinds}"


def test_real_deactivated_releases_immediately():
    rec = Recorder()
    det = HoldDetector(rec.press, rec.release)
    det.activated()
    det.deactivated()
    assert det.compositor_sends_deactivated
    assert rec.kinds == ["press", "release"]
    assert not det.held


def test_timing_fallback_survives_a_deactivated():
    """A compositor that sends Deactivated only sometimes must not strand us.

    Disabling the timing fallback the first time a Deactivated arrives would
    leave the microphone open until the safety watchdog fired if the next key-up
    were not reported. The fallback therefore stays armed permanently.
    """
    rec = Recorder()
    det = HoldDetector(rec.press, rec.release)
    det.activated()
    det.deactivated()
    assert rec.kinds == ["press", "release"]

    # Now a hold with no Deactivated at all: it must still release on timing.
    run_schedule(det, [0], INITIAL_GAP_MS + 250)
    assert rec.kinds == ["press", "release", "press", "release"], (
        f"timing fallback did not fire after a Deactivated; got {rec.kinds}"
    )
    assert not det.held


def test_cancel_suppresses_the_release_callback():
    rec = Recorder()
    det = HoldDetector(rec.press, rec.release)
    det.activated()
    det.cancel()
    assert rec.kinds == ["press"]
    loop = GLib.MainLoop()
    GLib.timeout_add(INITIAL_GAP_MS + 200, lambda: (loop.quit(), False)[1])
    loop.run()
    assert rec.kinds == ["press"], "cancelled hold still fired a release"
