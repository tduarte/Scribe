"""Exercise the worker protocol end to end against a stub worker."""
import os, struct, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from gi.repository import GLib
from transcriber import Transcriber

STUB = [sys.executable, os.path.join(os.path.dirname(__file__), "stub_worker.py")]
AUDIO = b"".join(struct.pack("<f", 0.1) for _ in range(1600))


def pump(predicate, timeout_ms=5000):
    """Run the main loop until predicate() is true or we give up."""
    loop = GLib.MainLoop()
    state = {"ok": False}

    def check():
        if predicate():
            state["ok"] = True
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(10, check)
    GLib.timeout_add(timeout_ms, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    loop.run()
    return state["ok"]


class Harness:
    def __init__(self):
        self.segments, self.results, self.errors, self.states = [], [], [], []
        self.t = Transcriber(
            STUB,
            on_segment=self.segments.append,
            on_result=lambda text, lang, ms: self.results.append((text, lang, ms)),
            on_error=self.errors.append,
            on_state=self.states.append,
        )


@pytest.fixture
def h():
    harness = Harness()
    yield harness
    harness.t.stop()


def test_worker_starts_and_reports_ready(h):
    assert h.t.start()
    assert pump(lambda: "ready" in h.states)


def test_preload_reports_the_loaded_model(h):
    h.t.preload("/models/turbo.bin")
    assert pump(lambda: h.t.loaded_model == "/models/turbo.bin")
    assert h.t.system_info == "STUB"


def test_transcribe_streams_segments_then_a_result(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    assert h.t.transcribe(AUDIO, model_path="/models/turbo.bin")
    assert pump(lambda: h.results)
    assert h.segments == ["hello ", "world"]
    assert h.results[0] == ("hello world", "en", 42)


def test_busy_flag_clears_after_a_result(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin")
    assert h.t.busy
    pump(lambda: h.results)
    assert not h.t.busy


def test_second_request_while_busy_is_refused(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin")
    assert h.t.transcribe(AUDIO, model_path="/m.bin") is False
    pump(lambda: h.results)


def test_worker_error_is_surfaced(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin", fail=True)
    assert pump(lambda: h.errors)
    assert "stub failure" in h.errors[0]
    assert not h.t.busy


def test_worker_crash_is_reported_and_not_fatal(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin", crash=True)
    assert pump(lambda: h.errors), "a crashed worker produced no error callback"
    assert not h.t.busy
    # The next request must transparently respawn the worker.
    h.errors.clear(); h.results.clear()
    assert h.t.transcribe(AUDIO, model_path="/m.bin")
    assert pump(lambda: h.results), "worker was not respawned after a crash"
    assert h.results[0][0] == "hello world"


def test_staged_audio_is_deleted_once_transcribed(h):
    """The recording must not outlive the transcription that consumed it."""
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin")
    pump(lambda: h.results)
    assert not os.path.exists(h.t._audio_path), (
        "the staged recording was left behind after transcription"
    )


def test_staged_audio_is_deleted_when_the_worker_crashes(h):
    """A crash must not leave the recording sitting in the runtime directory."""
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin", crash=True)
    assert pump(lambda: h.errors)
    assert not os.path.exists(h.t._audio_path), (
        "the staged recording survived a worker crash"
    )


def test_staged_audio_is_deleted_even_when_transcription_fails(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin", fail=True)
    assert pump(lambda: h.errors)
    assert not os.path.exists(h.t._audio_path), (
        "the staged recording survived a failed transcription"
    )
