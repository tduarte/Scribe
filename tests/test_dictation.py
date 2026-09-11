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


class TestMicrophoneFailure:
    def test_a_pipeline_that_dies_mid_recording_is_reported(self):
        ctl, p, _ = build(recorder=FakeRecorder(error_after_start="Device 'hw:2' vanished"))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        assert ctl.state is State.IDLE
        assert ctl.last_error == "Device 'hw:2' vanished"
        assert sounds.ERROR in p["player"].played
        assert p["notifier"].sent, "the user was not told the microphone failed"
        assert p["transcriber"].requests == []

    def test_a_genuinely_short_recording_is_still_quiet(self):
        ctl, p, _ = build(recorder=FakeRecorder(seconds=0.05))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        assert ctl.state is State.IDLE
        assert sounds.ERROR not in p["player"].played
        assert p["notifier"].sent == []


class TestCancel:
    """Cancelling must be final: nothing from that dictation may surface later."""

    def test_cancel_while_transcribing_abandons_the_job(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.cancel()
        assert ctl.state is State.IDLE
        assert p["transcriber"].cancelled == 1

    def test_a_result_after_cancel_is_not_pasted(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.cancel()
        ctl.on_result("too late", "en", 10)
        assert p["injector"].pasted == []
        assert p["history"].entries == []
        assert ctl.state is State.IDLE
        assert sounds.DONE not in p["player"].played

    def test_a_result_during_the_next_recording_is_ignored(self):
        # The stale result must not end a recording that is still going on:
        # the release that follows has to find the state it expects.
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.cancel()
        ctl.on_shortcut_press()
        ctl.on_result("too late", "en", 10)
        assert ctl.state is State.RECORDING
        assert p["injector"].pasted == []
        ctl.on_shortcut_release()
        assert ctl.state is State.TRANSCRIBING
        assert len(p["transcriber"].requests) == 2

    def test_partials_after_cancel_are_dropped(self):
        partials = []
        ctl, p, _ = build(on_partial=partials.append)
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.cancel()
        ctl.on_segment("ghost")
        assert partials == []

    def test_a_late_error_after_cancel_is_ignored(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.cancel()
        played = list(p["player"].played)
        ctl.on_error("worker went away")
        assert p["player"].played == played
        assert ctl.last_error == ""

    def test_the_model_is_fixed_when_recording_starts(self):
        # Switching models mid-utterance must not change what transcribes it.
        settings = FakeSettings()
        ctl, p, _ = build(settings=settings)
        ctl.on_shortcut_press()
        settings.set("active-model", "small")
        ctl.on_shortcut_release()
        req = p["transcriber"].requests[0]
        assert req["model_path"] == "/models/ggml-large-v3-turbo-q5_0.bin"
        ctl.on_result("hello", "en", 10)
        assert p["history"].entries[0][2] == "turbo"


class TestLanguageAwareFillers:
    def test_whispers_language_decides_the_filler_list(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("ah, er ist da", "de", 10)
        assert p["injector"].pasted[0]["text"] == "Ah, er ist da"

    def test_translation_output_is_english(self):
        ctl, p, _ = build(settings=FakeSettings(**{"translate-to-english": True}))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("er, okay", "de", 10)
        assert p["injector"].pasted[0]["text"] == "Okay"

    def test_the_configured_language_is_the_fallback(self):
        ctl, p, _ = build(settings=FakeSettings(language="de"))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("ah, er ist da", "", 10)
        assert p["injector"].pasted[0]["text"] == "Ah, er ist da"


class TestResultHandlerFailure:
    def test_a_bug_while_handling_the_result_does_not_strand_the_state_machine(self):
        ctl, p, _ = build()

        def explode(*a, **kw):
            raise RuntimeError("history is broken")

        p["history"].add = explode
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello", "en", 10)
        assert ctl.state is State.IDLE
        assert sounds.ERROR in p["player"].played
        # And the next dictation goes through as usual.
        ctl.on_shortcut_press()
        assert ctl.state is State.RECORDING


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


class TestPreload:
    """The model has to load while the user speaks, not after they stop."""

    def test_pressing_starts_loading_the_model(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press()
        assert p["transcriber"].preloads == [
            {"model_path": "/models/ggml-large-v3-turbo-q5_0.bin",
             "use_gpu": True, "threads": 0}
        ]

    def test_preload_matches_what_the_transcription_asks_for(self):
        # The worker keys its resident model on these, so a mismatch would load
        # the model twice and hand back none of the time the preload bought.
        ctl, p, _ = build(settings=FakeSettings(**{"accelerator": "cpu",
                                                   "thread-count": 4}))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        pre = p["transcriber"].preloads[0]
        req = p["transcriber"].requests[0]
        assert pre["model_path"] == req["model_path"]
        assert (pre["use_gpu"], pre["threads"]) == (req["use_gpu"], req["threads"])

    def test_nothing_is_loaded_when_the_microphone_fails(self):
        ctl, p, _ = build(recorder=FakeRecorder(fail=True))
        ctl.on_shortcut_press()
        assert p["transcriber"].preloads == []

    def test_a_worker_that_cannot_start_leaves_us_idle(self):
        # The failure arrives from inside start_recording, so it must not be
        # overwritten by the RECORDING state it interrupted.
        ctl, p, _ = build()
        p["transcriber"].on_preload = lambda: ctl.on_error("worker would not start")
        ctl.on_shortcut_press()
        assert ctl.state is State.IDLE
        assert ctl.last_error == "worker would not start"
        assert p["recorder"].cancelled == 1

    def test_nothing_is_loaded_without_a_model(self):
        ctl, p, _ = build(model_store=FakeModels(downloaded=False))
        ctl.on_shortcut_press()
        assert p["transcriber"].preloads == []


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

    def test_history_limit_is_enforced_on_every_write(self):
        # The limit is a privacy guarantee, so the database must never hold
        # more than the user asked for, even between dictations.
        ctl, p, _ = build(settings=FakeSettings(**{"history-limit": 3}))
        for i in range(5):
            ctl.on_shortcut_press(); ctl.on_shortcut_release()
            ctl.on_result(f"utterance {i}", "en", 10)
        assert p["history"].limits_applied == [3, 3, 3, 3, 3]
        assert len(p["history"].entries) == 3
        assert [e[0] for e in p["history"].entries] == [
            "Utterance 2", "Utterance 3", "Utterance 4"
        ]

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
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_segment("hello ")
        ctl.on_segment("world")
        assert partials == ["hello ", "hello world"]


class TestModelUnload:
    def test_unload_is_scheduled_after_a_stray_keypress(self):
        ctl, p, _ = build(settings=FakeSettings(**{"model-unload-seconds": 60}),
                          recorder=FakeRecorder(seconds=0.05))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        assert ctl._unload_timer is not None

    def test_unload_is_scheduled_after_a_failure(self):
        ctl, p, _ = build(settings=FakeSettings(**{"model-unload-seconds": 60}))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_error("boom")
        assert ctl._unload_timer is not None

    def test_unload_is_scheduled_after_delivery(self):
        ctl, p, _ = build(settings=FakeSettings(**{"model-unload-seconds": 60}))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello", "en", 10)
        assert ctl._unload_timer is not None

    def test_pressing_again_cancels_the_pending_unload(self):
        ctl, p, _ = build(settings=FakeSettings(**{"model-unload-seconds": 60}))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello", "en", 10)
        ctl.on_shortcut_press()
        assert ctl._unload_timer is None


class TestWatchdog:
    def test_watchdog_stops_a_stuck_recording(self):
        # GNOME never sends key-up, so a dropped repeat must not leave the mic on.
        ctl, p, _ = build(settings=FakeSettings(**{"max-recording-seconds": 5}))
        ctl.on_shortcut_press()
        assert ctl._watchdog is not None
        ctl._on_watchdog()
        assert ctl.state is State.TRANSCRIBING
        assert len(p["transcriber"].requests) == 1

    def test_release_hands_the_watchdog_to_the_transcription(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press()
        assert ctl._guarding is State.RECORDING
        ctl.on_shortcut_release()
        assert ctl._guarding is State.TRANSCRIBING
        assert ctl._watchdog is not None

    def test_watchdog_gives_up_on_a_hung_transcription(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl._on_watchdog()
        assert ctl.state is State.IDLE
        assert p["transcriber"].cancelled == 1
        assert sounds.ERROR in p["player"].played
        assert "too long" in ctl.last_error
        # A result that trickles in afterwards is not pasted.
        ctl.on_result("late", "en", 10)
        assert p["injector"].pasted == []

    def test_watchdog_gives_up_on_a_hung_delivery(self):
        ctl, p, _ = build(injector=FakeInjector(hang=True))
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello", "en", 10)
        assert ctl.state is State.DELIVERING
        assert ctl._guarding is State.DELIVERING
        ctl._on_watchdog()
        assert ctl.state is State.IDLE
        assert p["injector"].aborted == 1
        assert "too long" in ctl.last_error
        # The portal finally answering must not play the done chime on an
        # idle controller.
        played = list(p["player"].played)
        p["injector"].pending(True, "")
        assert p["player"].played == played
        assert ctl.state is State.IDLE

    def test_every_busy_state_has_a_deadline(self):
        ctl, _, _ = build(settings=FakeSettings(**{"max-recording-seconds": 60}))
        assert ctl._watchdog_seconds(State.RECORDING) == 60
        assert ctl._watchdog_seconds(State.TRANSCRIBING) == 180
        assert ctl._watchdog_seconds(State.DELIVERING) == 20

    def test_the_watchdog_is_gone_once_idle(self):
        ctl, p, _ = build()
        ctl.on_shortcut_press(); ctl.on_shortcut_release()
        ctl.on_result("hello", "en", 10)
        assert ctl.state is State.IDLE
        assert ctl._watchdog is None
        assert ctl._guarding is None


class TestExtraBuffer:
    def test_extra_buffer_delays_the_stop(self):
        ctl, p, _ = build(settings=FakeSettings(**{"extra-buffer-ms": 40}))
        ctl.on_shortcut_press()
        ctl.on_shortcut_release()
        assert ctl.state is State.RECORDING, "should still be recording during the tail"
        settle(120)
        assert ctl.state is State.TRANSCRIBING
