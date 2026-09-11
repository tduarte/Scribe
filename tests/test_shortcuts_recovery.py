"""The shortcut session comes back after the portal closes it."""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gi.repository import GLib

from portals.base import PortalError
from portals.shortcuts import RETRY_MS, RETRY_MAX_MS, ShortcutManager


class FakePortal:
    def __init__(self):
        self.calls = []
        self.closed = []
        self.watched = []
        self.unsubscribed = []
        self._next = 1

    def subscribe_signal(self, iface, signal, handler):
        self._next += 1
        return self._next

    def unsubscribe(self, sub):
        self.unsubscribed.append(sub)

    def watch_session_closed(self, handle, handler):
        self.watched.append((handle, handler))
        return 1000 + len(self.watched)

    def request_call(self, iface, method, build_args, callback):
        self.calls.append((method, callback))

    def close_session(self, handle):
        self.closed.append(handle)


def manager():
    errors, changes = [], []
    m = ShortcutManager(lambda: None, lambda: None, errors.append)
    m.portal = FakePortal()
    m.on_triggers_changed = lambda: changes.append(dict(m.triggers))
    return m, errors, changes


def bring_up(m, handle="/s/1"):
    method, cb = m.portal.calls[-1]
    assert method == "CreateSession"
    cb({"session_handle": handle}, None)
    method, cb = m.portal.calls[-1]
    assert method == "BindShortcuts"
    cb({"shortcuts": [("dictate", {"trigger_description": "Ctrl+Alt+Space"})]}, None)


def test_the_session_is_watched_for_closure():
    m, _, _ = manager()
    m.start()
    bring_up(m)
    assert [h for h, _ in m.portal.watched] == ["/s/1"]
    assert m.triggers == {"dictate": "Ctrl+Alt+Space"}


def test_a_closed_session_drops_the_trigger_and_schedules_a_retry():
    m, errors, changes = manager()
    m.start()
    bring_up(m)
    m.detector._held = True
    _, on_closed = m.portal.watched[-1]
    on_closed()
    assert m.session is None
    assert m.triggers == {}
    assert changes[-1] == {}, "the window was not told the shortcut is gone"
    assert not m.detector.held
    assert m._retry is not None
    assert m.portal.closed == [], "a handle the portal already closed must not be closed again"
    m.stop()


def test_the_retry_recreates_the_session_and_the_backoff_resets_on_success():
    m, _, _ = manager()
    m.start()
    bring_up(m)
    _, on_closed = m.portal.watched[-1]
    on_closed()
    assert m._retry_ms == RETRY_MS * 2
    m._on_retry()                       # what the timer would do
    assert m.portal.calls[-1][0] == "CreateSession"
    bring_up(m, "/s/2")
    assert m.session == "/s/2"
    assert m._retry_ms == RETRY_MS
    assert len(m.portal.calls) == 4
    m.stop()


def test_a_refused_session_retries_with_a_growing_delay_up_to_the_cap():
    m, errors, _ = manager()
    m.start()
    delays = []
    for _ in range(8):
        delays.append(m._retry_ms)
        _, cb = m.portal.calls[-1]
        cb(None, PortalError("CreateSession: portal not running"))
        assert m._retry is not None
        m._on_retry()
    assert delays == [3000, 6000, 12000, 24000, 48000, 60000, 60000, 60000]
    assert len(errors) == 8
    assert max(delays) == RETRY_MAX_MS
    m.stop()


def test_a_user_refusal_is_not_retried():
    m, errors, _ = manager()
    m.start()
    _, cb = m.portal.calls[-1]
    cb(None, PortalError("BindShortcuts: cancelled by the user", cancelled=True))
    assert m._retry is None
    assert errors and errors[0].cancelled


def test_restarting_does_not_subscribe_twice():
    m, _, _ = manager()
    m.start()
    m.start()
    assert len(m._subs) == 3


def test_stop_cancels_a_pending_retry():
    m, _, _ = manager()
    m.start()
    _, cb = m.portal.calls[-1]
    cb(None, PortalError("nope"))
    assert m._retry is not None
    m.stop()
    assert m._retry is None
