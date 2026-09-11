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


def test_cancel_abandons_the_job_and_the_next_request_still_works(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin", delay_ms=3000)
    assert h.t.busy
    h.t.cancel()
    assert not h.t.busy
    assert h.t.transcribe(AUDIO, model_path="/m.bin")
    assert pump(lambda: h.results), "worker was not respawned after a cancel"
    assert h.results == [("hello world", "en", 42)]
    assert h.errors == [], "a cancel must not be reported as a failure"


def test_output_from_a_cancelled_worker_is_never_dispatched(h):
    # The old worker may still get its result out before it is killed; that
    # line belongs to a stream we have abandoned.
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin", delay_ms=200)
    h.t.cancel()
    h.t.transcribe(AUDIO, model_path="/m.bin")
    assert pump(lambda: h.results)
    pump(lambda: False, timeout_ms=700)   # long enough for the old one to speak
    assert len(h.results) == 1
    assert h.segments == ["hello ", "world"]


def test_a_raising_result_handler_does_not_end_the_protocol(h):
    calls = []

    def explode(text, lang, ms):
        calls.append(text)
        raise RuntimeError("bug in the handler")

    h.t._on_result = explode
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin")
    assert pump(lambda: calls)
    assert not h.t.busy
    assert not os.path.exists(h.t._audio_path)
    # The worker is still alive and readable: the next request completes.
    assert h.t.transcribe(AUDIO, model_path="/m.bin")
    assert pump(lambda: len(calls) == 2), "the read loop died with the handler"


def test_a_raising_segment_handler_fails_the_job_and_keeps_the_worker(h):
    def explode(text):
        raise RuntimeError("bug in the handler")

    h.t._on_segment = explode
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin")
    assert pump(lambda: h.errors), "the handler's failure was not reported"
    assert not h.t.busy
    h.t._on_segment = h.segments.append
    assert h.t.transcribe(AUDIO, model_path="/m.bin")
    assert pump(lambda: h.results), "the read loop died with the handler"


def test_a_broken_pipe_gives_up_and_respawns_on_the_next_request(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin", delay_ms=3000)
    old = h.t._proc
    # Swap in a stream whose every read fails (a directory cannot be read),
    # the way a torn-down pipe would, without touching the real one mid-read.
    from gi.repository import Gio
    fd = os.open(os.path.dirname(__file__), os.O_RDONLY)
    h.t._stdout = Gio.DataInputStream.new(Gio.UnixInputStream.new(fd, False))
    h.t._read_line()
    assert pump(lambda: h.errors), "losing the pipe was not reported"
    assert not h.t.busy
    assert h.t._proc is None
    assert h.t.transcribe(AUDIO, model_path="/m.bin")
    assert h.t._proc is not old
    assert pump(lambda: h.results)
    os.close(fd)


def test_stop_while_busy_kills_the_worker_at_once(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t.transcribe(AUDIO, model_path="/m.bin", delay_ms=3000)
    proc = h.t._proc
    h.t.stop()
    assert pump(lambda: proc.get_if_exited() or proc.get_if_signaled(), timeout_ms=1000), (
        "a busy worker was left running after stop()"
    )


def test_a_failed_send_discards_the_staged_audio(h):
    h.t.start()
    pump(lambda: "ready" in h.states)
    h.t._stdin.close(None)
    assert h.t.transcribe(AUDIO, model_path="/m.bin") is False
    assert not os.path.exists(h.t._audio_path)
