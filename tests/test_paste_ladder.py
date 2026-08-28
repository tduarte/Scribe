"""The escalating paste: send a chord, and try the next one only if nothing
read the clipboard.

These drive the state machine directly rather than through a portal, because
the interesting behaviour is the sequencing and the eager-reader guard. The
injector is built with __new__ so no D-Bus session is needed; only the fields
the ladder touches are filled in.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gi.repository import GLib

from portals.inject import EAGER_PULLS, LADDER, SETTLE_MS, TextInjector


class Ladder:
    """A TextInjector reduced to its paste ladder, with chords recorded."""

    def __init__(self, receipt_on=None, baseline=0):
        # receipt_on: the chord that "pastes", i.e. bumps the transfer count.
        self.receipt_on = receipt_on
        self.sent = []
        self.done = []
        self.restored = []

        inj = TextInjector.__new__(TextInjector)
        inj.transfers = baseline
        inj._saved = b"previous clipboard"
        inj.send_chord = self._send
        inj._restore = lambda saved: self.restored.append(saved)
        self.inj = inj

    def _send(self, chord):
        self.sent.append(chord)
        if chord == self.receipt_on:
            self.inj.transfers += 1

    def run(self, chord="ctrl-v", escalate=True, delay_ms=60):
        loop = GLib.MainLoop()
        self.inj._start_ladder(
            chord, escalate, delay_ms,
            lambda ok, why: self.done.append((ok, why)),
        )
        # Long enough for every rung plus the restore that follows the last one.
        GLib.timeout_add(
            SETTLE_MS * (len(LADDER) + 1) + 400, lambda: (loop.quit(), False)[1]
        )
        loop.run()


def test_stops_at_the_first_chord_that_pastes():
    lad = Ladder(receipt_on=LADDER[0], baseline=EAGER_PULLS)
    lad.run()
    assert lad.sent == [LADDER[0]]
    assert lad.done == [(True, "")]


def test_the_compositors_own_eager_pull_does_not_look_like_a_paste():
    # mutter reads the selection once by itself. Counting that as a receipt
    # would stop the ladder at the first rung, everywhere.
    lad = Ladder(receipt_on=None, baseline=EAGER_PULLS)
    lad.run()
    assert lad.sent == list(LADDER)


def test_escalates_past_a_chord_nothing_read():
    lad = Ladder(receipt_on=LADDER[1])
    lad.run()
    assert lad.sent == list(LADDER[:2])
    assert lad.done == [(True, "")]


def test_runs_the_whole_ladder_when_nothing_ever_pastes():
    lad = Ladder(receipt_on=None)
    lad.run()
    assert lad.sent == list(LADDER)
    ok, why = lad.done[0]
    assert not ok and why


def test_ctrl_v_is_the_last_rung():
    # The only rung whose failure is visible: in a terminal it is quoted-insert
    # and leaves ^V on the prompt. Anything else must be tried first.
    assert LADDER[-1] == "ctrl-v"


def test_pinned_chord_sends_only_that_chord():
    lad = Ladder(receipt_on=None)
    lad.run(chord="shift-insert", escalate=False)
    assert lad.sent == ["shift-insert"]
    # A single rung cannot tell a missing receipt from an app that never
    # pastes, so it keeps reporting success as it did before the ladder.
    assert lad.done == [(True, "")]


def test_a_read_before_the_first_chord_disables_escalation():
    # A clipboard manager reads every new selection, so a receipt would no
    # longer mean an application pasted -- and escalating on it would paste
    # three times.
    lad = Ladder(receipt_on=None, baseline=EAGER_PULLS + 1)
    lad.run(chord="ctrl-shift-v")
    assert lad.sent == ["ctrl-shift-v"]
    assert lad.done == [(True, "")]


def test_clipboard_is_restored_once_after_the_ladder_ends():
    lad = Ladder(receipt_on=LADDER[1])
    lad.run()
    assert lad.restored == [b"previous clipboard"]


def test_a_failing_chord_stops_the_ladder_and_reports_why():
    lad = Ladder(receipt_on=None)

    def boom(chord):
        lad.sent.append(chord)
        raise GLib.Error.new_literal(GLib.quark_from_string("test"), "no session", 0)

    lad.inj.send_chord = boom
    lad.run()
    assert lad.sent == [LADDER[0]]
    ok, why = lad.done[0]
    assert not ok and "no session" in why
