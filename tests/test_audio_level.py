import os, struct, sys, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from audio import _rms


def pcm(samples):
    return b"".join(struct.pack("<f", s) for s in samples)


def test_silence_is_zero():
    assert _rms(pcm([0.0] * 128)) == 0.0


def test_empty_buffer_is_safe():
    assert _rms(b"") == 0.0
    assert _rms(b"\x00\x00") == 0.0


def test_full_scale_is_one():
    assert _rms(pcm([1.0, -1.0] * 64)) == 1.0


def test_louder_reads_higher():
    quiet = _rms(pcm([0.01, -0.01] * 64))
    loud = _rms(pcm([0.5, -0.5] * 64))
    assert 0.0 < quiet < loud < 1.0


def test_result_always_in_range():
    for amp in (1e-9, 1e-3, 0.1, 0.9, 1.0, 5.0):
        v = _rms(pcm([amp, -amp] * 32))
        assert 0.0 <= v <= 1.0, f"amp={amp} gave {v}"


def test_truncated_trailing_bytes_ignored():
    # A buffer that is not a whole number of float32 frames must not raise.
    assert 0.0 <= _rms(pcm([0.3] * 10) + b"\x01\x02") <= 1.0


# -- threading ---------------------------------------------------------------
#
# appsink delivers buffers on its own streaming thread. Nothing that reaches
# GTK may run there, so the recorder must hand the cue and the meter over to
# the main loop.

import threading
from gi.repository import GLib
from audio import Recorder


def spin(ms=50):
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    loop.run()


def test_ready_cue_is_delivered_on_the_main_loop_not_the_audio_thread():
    seen = []
    rec = Recorder(on_ready=lambda: seen.append(threading.current_thread()))
    with rec._lock:
        rec._capturing = True

    worker = threading.Thread(target=rec._ingest, args=(pcm([0.2] * 64),))
    worker.start(); worker.join()
    assert seen == [], "cue fired synchronously on the streaming thread"

    spin()
    assert seen == [threading.main_thread()]


def test_ready_cue_fires_once_per_recording():
    seen = []
    rec = Recorder(on_ready=lambda: seen.append(1))
    with rec._lock:
        rec._capturing = True
    rec._ingest(pcm([0.2] * 64))
    rec._ingest(pcm([0.2] * 64))
    spin()
    assert seen == [1]


def test_ready_cue_is_dropped_if_the_recording_ended_first():
    seen = []
    rec = Recorder(on_ready=lambda: seen.append(1))
    with rec._lock:
        rec._capturing = True
    rec._ingest(pcm([0.2] * 64))
    rec.cancel()
    spin()
    assert seen == []


def test_level_is_sampled_by_a_main_loop_timer():
    seen = []
    rec = Recorder(on_level=lambda lvl: seen.append(threading.current_thread()))
    worker = threading.Thread(target=rec._ingest, args=(pcm([0.5] * 64),))
    worker.start(); worker.join()
    assert seen == [], "meter updated synchronously on the streaming thread"

    rec._start_level_timer()
    try:
        spin(120)
    finally:
        rec._stop_level_timer()
    assert seen and set(seen) == {threading.main_thread()}
    # One buffer, one reading: the timer must not replay a stale buffer.
    assert len(seen) == 1


def test_buffers_land_in_the_recording_they_belong_to():
    rec = Recorder()
    with rec._lock:
        rec._capturing = True
    rec._ingest(b"A" * 8)
    first = rec.stop()
    rec._ingest(b"B" * 8)          # arrives after stop: not part of anything
    with rec._lock:
        rec._chunks = []; rec._ready_fired = False; rec._capturing = True
    rec._ingest(b"C" * 8)
    second = rec.stop()
    assert (first, second) == (b"A" * 8, b"C" * 8)


# -- pipeline errors ---------------------------------------------------------


class FakeMessage:
    def __init__(self, text):
        self.text = text

    def parse_error(self):
        class Err:
            message = self.text
        return Err(), "debug details"


class FakePipeline:
    def __init__(self):
        self.states = []

    def set_state(self, state):
        self.states.append(state)


class FakeBus:
    def __init__(self):
        self.removed = 0

    def remove_signal_watch(self):
        self.removed += 1


def test_a_bus_error_tears_the_pipeline_down_and_keeps_the_audio():
    from gi.repository import Gst
    rec = Recorder()
    rec._pipeline, rec._bus = FakePipeline(), FakeBus()
    pipeline, bus = rec._pipeline, rec._bus
    with rec._lock:
        rec._capturing = True
    rec._ingest(pcm([0.2] * 64))

    rec._on_bus_error(bus, FakeMessage("Could not open audio device"))

    assert rec.last_error == "Could not open audio device"
    assert rec._pipeline is None, "the dead pipeline was kept for reuse"
    assert pipeline.states == [Gst.State.NULL]
    assert bus.removed == 1
    assert not rec._capturing
    assert rec.stop() == pcm([0.2] * 64), "audio captured before the error was lost"


def test_starting_a_recording_forgets_the_previous_error():
    rec = Recorder()
    rec.last_error = "old news"
    rec.warm_up = lambda device="": True
    assert rec.start()
    assert rec.last_error is None
