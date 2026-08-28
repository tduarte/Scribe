"""Whisper inference, isolated in its own process.

Two things force this out of the UI process:

* The model must stay resident. Reloading a 574 MB model for every utterance
  would add seconds to each dictation, which defeats the point.
* A crash in native GPU code would otherwise take the whole app down. Vulkan
  driver faults are not hypothetical on new hardware.

The protocol is newline-delimited JSON on stdin/stdout, so the parent can read it
with Gio.DataInputStream on the GLib main loop with no threads. Audio is handed
over as a raw float32 file rather than inline, to keep messages small.

stdout carries protocol only. Anything diagnostic goes to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

# stdout is the protocol channel. whisper.cpp's own logging and pywhispercpp's
# print_progress both default to writing there, so both are redirected to stderr
# in Engine.load(); nothing else may print to stdout.


def emit(**payload) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(message: str) -> None:
    sys.stderr.write(f"[worker] {message}\n")
    sys.stderr.flush()


class Engine:
    def __init__(self) -> None:
        self.model = None
        self.model_path: str | None = None
        self.use_gpu = True
        self.threads = 0
        self._Model = None
        self._supported: set[str] = set()

    def _import(self):
        if self._Model is None:
            from pywhispercpp.model import Model  # imported lazily: it is expensive
            self._Model = Model
            self._supported = self._discover_params()
        return self._Model

    def _discover_params(self) -> set[str]:
        """Which transcribe params this pywhispercpp build actually accepts.

        The binding tracks whisper.cpp's params struct, which gains fields over
        time. Passing an unknown one raises, so we filter rather than guess.
        """
        try:
            from pywhispercpp import constants
            schema = getattr(constants, "PARAMS_SCHEMA", None)
            if schema:
                return set(schema.keys())
        except Exception as exc:  # pragma: no cover - depends on the build
            log(f"could not read PARAMS_SCHEMA: {exc}")
        return set()

    def _filter(self, params: dict) -> dict:
        if not self._supported:
            return params
        kept = {k: v for k, v in params.items() if k in self._supported}
        dropped = set(params) - set(kept)
        if dropped:
            log(f"dropping unsupported params: {sorted(dropped)}")
        return kept

    def load(self, msg: dict) -> None:
        path = msg["model"]
        use_gpu = bool(msg.get("use_gpu", True))
        threads = int(msg.get("threads") or 0)

        if (self.model is not None and path == self.model_path
                and use_gpu == self.use_gpu and threads == self.threads):
            emit(event="loaded", model=path, cached=True)
            return

        Model = self._import()
        self.unload()

        # Decode defaults that matter for a dictation app: progress printing
        # would corrupt the protocol stream, and timestamps are noise here.
        params = {
            "print_progress": False,
            "print_realtime": False,
            "print_timestamps": False,
            "no_timestamps": True,
        }
        if threads > 0:
            params["n_threads"] = threads

        self.model = Model(
            path,
            context_params={"use_gpu": use_gpu},
            redirect_whispercpp_logs_to=sys.stderr,
            **self._filter(params),
        )
        self.model_path = path
        self.use_gpu = use_gpu
        self.threads = threads

        info = ""
        try:
            info = self.model.system_info()
        except Exception:
            pass
        emit(event="loaded", model=path, cached=False, system_info=info)

    def unload(self) -> None:
        self.model = None
        self.model_path = None

    def transcribe(self, msg: dict) -> None:
        import numpy as np

        if (self.model is None or msg.get("model") != self.model_path
                or bool(msg.get("use_gpu", True)) != self.use_gpu):
            self.load(msg)

        audio = np.fromfile(msg["audio"], dtype=np.float32)
        if audio.size == 0:
            emit(event="result", text="", language="", duration_ms=0)
            return

        language = msg.get("language") or "auto"
        params = {"language": language, "translate": bool(msg.get("translate"))}
        if msg.get("vad") and msg.get("vad_model"):
            params["vad"] = True
            params["vad_model_path"] = msg["vad_model"]
            # Fine-grained VAD tuning is not exposed by this binding; _filter
            # drops anything the installed version does not understand.
            params["vad_threshold"] = float(msg.get("vad_threshold", 0.5))
            params["vad_min_silence_duration_ms"] = int(msg.get("vad_min_silence_ms", 100))
            params["vad_speech_pad_ms"] = int(msg.get("vad_speech_pad_ms", 30))
        params = self._filter(params)

        def on_segment(segment) -> None:
            # The callback receives a single Segment, not a list.
            try:
                emit(event="segment", text=getattr(segment, "text", ""))
            except Exception:
                pass

        started = time.monotonic()
        segments = self.model.transcribe(
            audio, new_segment_callback=on_segment, **params
        )
        text = "".join(getattr(s, "text", "") for s in segments)

        emit(
            event="result",
            text=text,
            language=language,
            duration_ms=int((time.monotonic() - started) * 1000),
            audio_ms=int(audio.size * 1000 / 16000),
        )


def main() -> int:
    engine = Engine()
    emit(event="ready", pid=os.getpid())

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            emit(event="error", message="malformed request")
            continue

        cmd = msg.get("cmd")
        try:
            if cmd == "transcribe":
                engine.transcribe(msg)
            elif cmd == "load":
                engine.load(msg)
            elif cmd == "unload":
                engine.unload()
                emit(event="unloaded")
            elif cmd == "ping":
                emit(event="pong")
            elif cmd == "quit":
                break
            else:
                emit(event="error", message=f"unknown command {cmd!r}")
        except Exception as exc:
            log(traceback.format_exc())
            emit(event="error", message=f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
