"""The dictation state machine: hotkey in, text into the focused app out.

    IDLE ──press──▶ RECORDING ──release──▶ TRANSCRIBING ──▶ DELIVERING ──▶ IDLE
      ▲                  │                      │               │
      └────── cancel ────┴──────────────────────┴───────────────┘

Everything runs on the GLib main loop. The only concurrency is the whisper
worker, which is a separate process reached over a pipe.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable

from gi.repository import GLib

import sounds
from postprocess import process as postprocess

log = logging.getLogger(__name__)


class State(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    DELIVERING = "delivering"


class DictationController:
    def __init__(
        self,
        *,
        settings,
        recorder,
        transcriber,
        injector,
        model_store,
        history,
        player,
        notifier=None,
        on_state: Callable[[State, str], None] | None = None,
        on_partial: Callable[[str], None] | None = None,
        on_level: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings
        self.recorder = recorder
        self.transcriber = transcriber
        self.injector = injector
        self.models = model_store
        self.history = history
        self.player = player
        self.notifier = notifier

        self._on_state = on_state
        self._on_partial = on_partial
        self.on_level = on_level

        self.state = State.IDLE
        self.last_text: str = ""
        self.last_error: str = ""
        self._partial: list[str] = []
        self._watchdog: int | None = None
        self._unload_timer: int | None = None
        self._audio_ms = 0

    # -- state -----------------------------------------------------------

    def _set_state(self, state: State, detail: str = "") -> None:
        if state is self.state and not detail:
            return
        self.state = state
        log.debug("state -> %s %s", state.value, detail)
        if self._on_state:
            self._on_state(state, detail)

    @property
    def active_model(self):
        return self.models.get(self.settings.get_string("active-model"))

    # -- recording -------------------------------------------------------

    def toggle(self) -> None:
        """Entry point for toggle mode and for the CLI/D-Bus toggle action."""
        if self.state is State.RECORDING:
            self.stop_recording()
        elif self.state is State.IDLE:
            self.start_recording()

    def on_shortcut_press(self) -> None:
        if self.settings.get_string("activation-mode") == "toggle":
            self.toggle()
        else:
            self.start_recording()

    def on_shortcut_release(self) -> None:
        # In toggle mode the release half of the hold is meaningless.
        if self.settings.get_string("activation-mode") == "toggle":
            return
        if self.state is State.RECORDING:
            self.stop_recording()

    def start_recording(self) -> None:
        if self.state is not State.IDLE:
            return
        model = self.active_model
        if model is None or not self.models.is_downloaded(model):
            self._fail("No speech model is installed yet.")
            return

        self._cancel_unload_timer()
        device = self.settings.get_string("input-device")
        self.recorder.keep_warm_seconds = self.settings.get_int("keep-stream-open-seconds")
        if not self.recorder.start(device):
            self._fail(self.recorder.last_error or "Could not open the microphone.")
            return

        self._partial.clear()
        self._set_state(State.RECORDING)
        self.player.play(sounds.START)
        self._arm_watchdog()

    def stop_recording(self) -> None:
        if self.state is not State.RECORDING:
            return
        self._disarm_watchdog()
        extra = self.settings.get_int("extra-buffer-ms")
        if extra > 0:
            GLib.timeout_add(extra, self._finish_recording)
        else:
            self._finish_recording()

    def _finish_recording(self) -> bool:
        if self.state is not State.RECORDING:
            return GLib.SOURCE_REMOVE
        audio = self.recorder.stop()
        self._audio_ms = int(len(audio) / 4 * 1000 / 16000)
        self.player.play(sounds.STOP)

        if len(audio) < 4 * 1600:  # under ~100 ms is a stray keypress, not speech
            self._set_state(State.IDLE)
            return GLib.SOURCE_REMOVE

        model = self.active_model
        if model is None:
            self._fail("No speech model is installed yet.")
            return GLib.SOURCE_REMOVE

        self._set_state(State.TRANSCRIBING)
        accel = self.settings.get_string("accelerator")
        options = {
            "language": self.settings.get_string("language"),
            "translate": self.settings.get_boolean("translate-to-english"),
            "use_gpu": accel != "cpu",
            "threads": self.settings.get_int("thread-count"),
            "vad": self.settings.get_boolean("vad-enabled"),
            "vad_model": self.models.vad_path() or "",
            "vad_threshold": self.settings.get_double("vad-threshold"),
            "vad_min_silence_ms": self.settings.get_int("vad-min-silence-ms"),
            "vad_speech_pad_ms": self.settings.get_int("vad-speech-pad-ms"),
        }
        if not self.transcriber.transcribe(
            audio, model_path=self.models.path_for(model), **options
        ):
            self._fail("Could not start transcription.")
        return GLib.SOURCE_REMOVE

    def cancel(self) -> None:
        self._disarm_watchdog()
        if self.state is State.RECORDING:
            self.recorder.cancel()
        self._partial.clear()
        self._set_state(State.IDLE, "cancelled")

    # -- watchdog --------------------------------------------------------

    def _arm_watchdog(self) -> None:
        self._disarm_watchdog()
        seconds = max(5, self.settings.get_int("max-recording-seconds"))
        self._watchdog = GLib.timeout_add_seconds(seconds, self._on_watchdog)

    def _disarm_watchdog(self) -> None:
        if self._watchdog is not None:
            GLib.source_remove(self._watchdog)
            self._watchdog = None

    def _on_watchdog(self) -> bool:
        self._watchdog = None
        if self.state is State.RECORDING:
            # GNOME never sends a key-up, so a dropped repeat could otherwise
            # leave the microphone open indefinitely. Treat this as a release.
            log.warning("recording hit the safety limit; stopping")
            self.stop_recording()
        return GLib.SOURCE_REMOVE

    # -- transcription callbacks ----------------------------------------

    def on_segment(self, text: str) -> None:
        self._partial.append(text)
        if self._on_partial:
            self._on_partial("".join(self._partial))

    def on_result(self, text: str, language: str, duration_ms: int) -> None:
        cleaned = postprocess(
            text,
            custom_words=list(self.settings.get_strv("custom-words")),
            word_threshold=self.settings.get_double("word-correction-threshold"),
            remove_filler_words=self.settings.get_boolean("remove-filler-words"),
            custom_fillers=list(self.settings.get_strv("custom-filler-words")),
            capitalize=self.settings.get_boolean("capitalize-first"),
            trailing_space=self.settings.get_boolean("append-trailing-space"),
        )
        self._partial.clear()

        if not cleaned:
            self._set_state(State.IDLE, "nothing was said")
            self._schedule_unload()
            return

        self.last_text = cleaned
        if self.settings.get_boolean("history-enabled"):
            model = self.active_model
            self.history.add(
                cleaned,
                duration_ms=self._audio_ms,
                model=model.id if model else "",
                language=language,
            )

        self._set_state(State.DELIVERING)
        self._deliver(cleaned)

    def on_error(self, message: str) -> None:
        self._fail(message)

    # -- delivery --------------------------------------------------------

    def _deliver(self, text: str) -> None:
        mode = self.settings.get_string("output-mode")

        def done(ok: bool, error: str) -> None:
            if ok:
                self._set_state(State.IDLE, "delivered")
            else:
                self._fail(error or "Could not insert the text.")
            self._schedule_unload()

        if mode == "clipboard":
            self.injector.copy_only(text, on_done=done)
            if self.notifier:
                preview = text if len(text) <= 80 else text[:77] + "..."
                self.notifier.notify(
                    "scribe-transcript", "Copied to clipboard", preview
                )
            return

        self.injector.paste(
            text,
            chord=self.settings.get_string("paste-chord"),
            restore_clipboard=self.settings.get_boolean("restore-clipboard"),
            delay_ms=self.settings.get_int("paste-delay-ms"),
            on_done=done,
        )

    # -- model lifetime --------------------------------------------------

    def _cancel_unload_timer(self) -> None:
        if self._unload_timer is not None:
            GLib.source_remove(self._unload_timer)
            self._unload_timer = None

    def _schedule_unload(self) -> None:
        self._cancel_unload_timer()
        seconds = self.settings.get_int("model-unload-seconds")
        if seconds > 0:
            self._unload_timer = GLib.timeout_add_seconds(seconds, self._do_unload)

    def _do_unload(self) -> bool:
        self._unload_timer = None
        if self.state is State.IDLE:
            self.transcriber.unload()
        return GLib.SOURCE_REMOVE

    # -- errors ----------------------------------------------------------

    def _fail(self, message: str) -> None:
        self.last_error = message
        log.error("%s", message)
        self.player.play(sounds.ERROR)
        self.recorder.cancel()
        self._set_state(State.IDLE, message)
        if self.notifier:
            self.notifier.notify(
                "scribe-error", "Dictation failed", message, priority="normal"
            )
