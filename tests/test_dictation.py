import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest
from gi.repository import GLib

import sounds
from dictation import DictationController, State
from fakes import (FakeHistory, FakeInjector, FakeModels, FakeNotifier,
                   FakePlayer, FakeRecorder, FakeSettings, FakeTranscriber)


def build(**kw):
    parts = {
        "settings": kw.pop("settings", FakeSettings()),
        "recorder": kw.pop("recorder", FakeRecorder()),
        "transcriber": kw.pop("transcriber", FakeTranscriber()),
        "injector": kw.pop("injector", FakeInjector()),
        "model_store": kw.pop("model_store", FakeModels()),
        "history": kw.pop("history", FakeHistory()),
        "player": kw.pop("player", FakePlayer()),
        "notifier": kw.pop("notifier", FakeNotifier()),
    }
    states = []
    ctl = DictationController(on_state=lambda s, d: states.append(s), **parts, **kw)
    return ctl, parts, states


def settle(ms=60):
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, lambda: (loop.quit(), GLib.SOURCE_REMOVE)[1])
    loop.run()


class TestRecording:
    def test_press_starts_recording(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press()
        assert ctl.state is State.RECORDING
        assert p["recorder"].started

    def test_start_cue_waits_for_real_audio(self):
        # Opening the microphone is not instant. Chiming before audio flows
        # invites the user to speak into a stream that is not up yet.
        ctl, p, _ = build()
        ctl.on_shortcut_press()
        assert p["player"].played == [], "chimed before the microphone was ready"
        p["recorder"].deliver_first_buffer()
        assert p["player"].played == [sounds.START]

    def test_start_cue_is_suppressed_if_recording_already_ended(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press()
        ctl.cancel()
        p["recorder"].deliver_first_buffer()
        assert sounds.START not in p["player"].played

    def test_release_transcribes(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press()
        ctl.on_shortcut_release()
        assert ctl.state is State.TRANSCRIBING
        assert len(p["transcriber"].requests) == 1

    def test_missing_model_refuses_to_record(self):
        ctl, p, _ = build(model_store=FakeModels(downloaded=False))
        ctl.on_shortcut_press()
        assert ctl.state is State.IDLE
        assert not p["recorder"].started
        assert p["player"].played == [sounds.ERROR]

    def test_microphone_failure_is_reported(self):
        ctl, p, _ = build(recorder=FakeRecorder(fail=True))
        ctl.on_shortcut_press()
        assert ctl.state is State.IDLE
        assert "microphone" in ctl.last_error

    def test_very_short_recording_is_discarded(self):
        # A stray keypress must not be sent to whisper.
        ctl, p, _ = build(recorder=FakeRecorder(seconds=0.05))
        ctl.on_shortcut_press()
        ctl.on_shortcut_release()
        assert ctl.state is State.IDLE
        assert p["transcriber"].requests == []

    def test_press_while_recording_is_ignored(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press()
        ctl.on_shortcut_press()
        assert ctl.state is State.RECORDING

    def test_cancel_discards_audio(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press()
        ctl.cancel()
        assert ctl.state is State.IDLE
        assert p["recorder"].cancelled == 1
        assert p["transcriber"].requests == []


class TestToggleMode:
    def test_toggle_starts_then_stops(self):
        ctl, p, _ = build(settings=FakeSettings(**{"activation-mode": "toggle"}))
        ctl.on_shortcut_press()
        assert ctl.state is State.RECORDING
        ctl.on_shortcut_press()
        assert ctl.state is State.TRANSCRIBING

    def test_release_is_ignored_in_toggle_mode(self):
        ctl, p, _ = build(settings=FakeSettings(**{"activation-mode": "toggle"}))
        ctl.on_shortcut_press()
        ctl.on_shortcut_release()
        assert ctl.state is State.RECORDING, "release must not stop a toggle recording"


class TestTranscriptionOptions:
    def test_vad_settings_are_forwarded(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        req = p["transcriber"].requests[0]
        assert req["vad"] is True
        assert req["vad_model"].endswith("ggml-silero-v6.2.0.bin")
        assert req["vad_threshold"] == 0.5

    def test_cpu_accelerator_disables_gpu(self):
        ctl, p, _ = build(settings=FakeSettings(**{"accelerator": "cpu"}))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        assert p["transcriber"].requests[0]["use_gpu"] is False


class TestResults:
    def test_result_is_cleaned_and_pasted(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("  um hello world ", "en", 100)
        assert p["injector"].pasted[0]["text"] == "Hello world"
        assert ctl.state is State.IDLE

    def test_completion_cue_plays_after_delivery(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello world", "en", 100)
        assert p["player"].played[-1] == sounds.DONE

    def test_no_completion_cue_when_delivery_fails(self):
        ctl, p, _ = build(injector=FakeInjector(ok=False, error="denied"))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello", "en", 10)
        assert sounds.DONE not in p["player"].played

    def test_result_is_recorded_in_history(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello world", "en", 100)
        assert p["history"].entries[0][0] == "Hello world"
        assert p["history"].entries[0][3] == "en"

    def test_history_can_be_disabled(self):
        ctl, p, _ = build(settings=FakeSettings(**{"history-enabled": False}))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello", "en", 10)
        assert p["history"].entries == []

    def test_silence_produces_no_output(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("[BLANK_AUDIO]", "en", 10)
        assert p["injector"].pasted == []
        assert p["history"].entries == []
        assert ctl.state is State.IDLE

    def test_clipboard_mode_copies_and_notifies(self):
        ctl, p, _ = build(settings=FakeSettings(**{"output-mode": "clipboard"}))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello world", "en", 10)
        assert p["injector"].copied == ["Hello world"]
        assert p["injector"].pasted == []
        assert p["notifier"].sent

    def test_injection_failure_is_surfaced(self):
        ctl, p, _ = build(injector=FakeInjector(ok=False, error="denied"))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello", "en", 10)
        assert ctl.state is State.IDLE
        assert ctl.last_error == "denied"
        assert sounds.ERROR in p["player"].played

    def test_worker_error_returns_to_idle(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_error("the transcription worker crashed")
        assert ctl.state is State.IDLE
        assert "crashed" in ctl.last_error

    def test_partial_segments_accumulate(self):
        partials = []
        ctl, p, _ = build(on_partial=partials.append)
        ctl.on_segment("hello ")
        ctl.on_segment("world")
        assert partials == ["hello ", "hello world"]


class TestWatchdog:
    def test_watchdog_stops_a_stuck_recording(self):
        # GNOME never sends key-up, so a dropped repeat must not leave the mic on.
        ctl, p, _ = build(settings=FakeSettings(**{"max-recording-seconds": 5}))
        ctl.on_shortcut_press()
        assert ctl._watchdog is not None
        ctl._on_watchdog()
        assert ctl.state is State.TRANSCRIBING
        assert len(p["transcriber"].requests) == 1

    def test_watchdog_is_disarmed_on_normal_release(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press()
        ctl.on_shortcut_release()
        assert ctl._watchdog is None


class TestExtraBuffer:
    def test_extra_buffer_delays_the_stop(self):
        ctl, p, _ = build(settings=FakeSettings(**{"extra-buffer-ms": 40}))
        ctl.on_shortcut_press()
        ctl.on_shortcut_release()
        assert ctl.state is State.RECORDING, "should still be recording during the tail"
        settle(120)
        assert ctl.state is State.TRANSCRIBING
