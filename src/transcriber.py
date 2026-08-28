"""Parent side of the whisper worker protocol.

Speaks newline-delimited JSON to the worker subprocess over a pipe, read
asynchronously on the GLib main loop. The worker is respawned automatically if it
dies, so a native crash costs one utterance rather than the session.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from typing import Callable

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(
        self,
        worker_argv: list[str],
        *,
        on_segment: Callable[[str], None] | None = None,
        on_result: Callable[[str, str, int], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self.worker_argv = worker_argv
        self._on_segment = on_segment
        self._on_result = on_result
        self._on_error = on_error
        self._on_state = on_state

        self._proc: Gio.Subprocess | None = None
        self._stdin: Gio.OutputStream | None = None
        self._stdout: Gio.DataInputStream | None = None
        self._ready = False
        self._busy = False
        self.loaded_model: str | None = None
        self.system_info: str = ""
        self._audio_path = os.path.join(
            GLib.get_user_runtime_dir() or "/tmp", "scribe-utterance.f32"
        )

    # -- process ---------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def start(self) -> bool:
        if self._proc is not None:
            return True
        try:
            self._proc = Gio.Subprocess.new(
                self.worker_argv,
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE,
            )
        except GLib.Error as exc:
            self._fail(f"could not start the transcription worker: {exc.message}")
            return False

        self._stdin = self._proc.get_stdin_pipe()
        self._stdout = Gio.DataInputStream.new(self._proc.get_stdout_pipe())
        self._read_line()
        self._proc.wait_async(None, self._on_exit)
        return True

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._send({"cmd": "quit"})
        except Exception:
            pass
        proc, self._proc = self._proc, None
        self._stdin = None
        self._stdout = None
        self._ready = False
        self._busy = False
        self.loaded_model = None
        GLib.timeout_add(500, lambda: (proc.force_exit(), GLib.SOURCE_REMOVE)[1])

    def _on_exit(self, proc, res) -> None:
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        if proc is not self._proc:
            return  # an orderly stop(), not a crash
        log.warning("transcription worker exited unexpectedly")
        self._proc = None
        self._stdin = None
        self._stdout = None
        self._ready = False
        self.loaded_model = None
        if self._busy:
            self._busy = False
            self._fail("the transcription worker crashed; it will be restarted")

    # -- protocol --------------------------------------------------------

    def _send(self, msg: dict) -> None:
        if self._stdin is None:
            raise RuntimeError("worker is not running")
        data = (json.dumps(msg) + "\n").encode("utf-8")
        self._stdin.write_all(data, None)
        self._stdin.flush()

    def _read_line(self) -> None:
        if self._stdout is None:
            return
        self._stdout.read_line_async(GLib.PRIORITY_DEFAULT, None, self._on_line)

    def _on_line(self, stream, res) -> None:
        try:
            raw, _ = stream.read_line_finish(res)
        except GLib.Error as exc:
            log.debug("worker read failed: %s", exc.message)
            return
        if raw is None:
            return  # pipe closed; _on_exit handles the rest

        try:
            msg = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            log.debug("ignoring non-protocol worker output: %r", raw[:120])
            self._read_line()
            return

        self._dispatch(msg)
        self._read_line()

    def _dispatch(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "ready":
            self._ready = True
            self._notify_state("ready")
        elif event == "loaded":
            self.loaded_model = msg.get("model")
            self.system_info = msg.get("system_info", "") or self.system_info
            self._notify_state("loaded")
        elif event == "segment":
            if self._on_segment:
                self._on_segment(msg.get("text", ""))
        elif event == "result":
            self._busy = False
            if self._on_result:
                self._on_result(
                    msg.get("text", ""),
                    msg.get("language", ""),
                    int(msg.get("duration_ms", 0)),
                )
        elif event == "unloaded":
            self.loaded_model = None
            self._notify_state("unloaded")
        elif event == "error":
            self._busy = False
            self._fail(msg.get("message", "unknown transcription error"))

    def _notify_state(self, state: str) -> None:
        if self._on_state:
            self._on_state(state)

    def _fail(self, message: str) -> None:
        log.error("%s", message)
        if self._on_error:
            self._on_error(message)

    # -- public API ------------------------------------------------------

    def preload(self, model_path: str, *, use_gpu: bool = True, threads: int = 0) -> None:
        if not self.start():
            return
        try:
            self._send({
                "cmd": "load", "model": model_path,
                "use_gpu": use_gpu, "threads": threads,
            })
        except Exception as exc:
            self._fail(str(exc))

    def unload(self) -> None:
        if self._proc is not None and not self._busy:
            try:
                self._send({"cmd": "unload"})
            except Exception:
                pass

    def transcribe(self, audio: bytes, *, model_path: str, **options) -> bool:
        """Hand a float32 PCM buffer to the worker. Results arrive via callbacks."""
        if self._busy:
            log.warning("transcription already in progress; ignoring")
            return False
        if not self.start():
            return False

        try:
            with open(self._audio_path, "wb") as fh:
                fh.write(audio)
        except OSError as exc:
            self._fail(f"could not stage audio: {exc}")
            return False

        request = {
            "cmd": "transcribe",
            "audio": self._audio_path,
            "model": model_path,
            **options,
        }
        try:
            self._send(request)
        except Exception as exc:
            self._fail(str(exc))
            return False
        self._busy = True
        return True
