"""Microphone capture, using the GStreamer already present in the GNOME runtime.

Whisper wants 16 kHz mono float32; GStreamer's audioconvert/audioresample give us
exactly that for free, whatever the microphone natively produces.

appsink hands buffers over on its own streaming thread, not on the GLib main
loop, so that thread does nothing but copy bytes under a lock. Everything the
rest of the app sees -- the ready cue, the level meter -- is delivered from the
main loop: the cue through an idle callback, the meter from a timer that samples
the most recent buffer at a fixed rate.

The pipeline can be left running between utterances ("keep warm"), because
spinning PipeWire up costs a few hundred milliseconds and that delay would
otherwise clip the first word.
"""

from __future__ import annotations

import array
import logging
import math
import threading
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
CAPS = (
    f"audio/x-raw,format=F32LE,channels={CHANNELS},"
    f"rate={SAMPLE_RATE},layout=interleaved"
)

# How often the level meter is refreshed. The meter is decoration; it does not
# need every buffer, and 30 Hz is well above what a level bar can show.
LEVEL_INTERVAL_MS = 33

_initialised = False


def init() -> None:
    global _initialised
    if not _initialised:
        Gst.init(None)
        _initialised = True


def list_devices() -> list[tuple[str, str]]:
    """Available audio sources as (device-id, display-name).

    The empty id means "system default" and is always offered first.
    """
    init()
    devices: list[tuple[str, str]] = [("", "System default")]
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Audio/Source", None)
    if monitor.start():
        for dev in monitor.get_devices():
            props = dev.get_properties()
            node = None
            if props:
                for key in ("node.name", "device.id", "api.alsa.path"):
                    node = props.get_string(key)
                    if node:
                        break
            devices.append((node or dev.get_display_name(), dev.get_display_name()))
        monitor.stop()
    return devices


class Recorder:
    """Captures microphone audio into a float32 buffer."""

    def __init__(
        self,
        *,
        on_level: Callable[[float], None] | None = None,
        on_ready: Callable[[], None] | None = None,
        keep_warm_seconds: int = 30,
    ) -> None:
        init()
        self._on_level = on_level
        # Fired when the first buffer of a recording actually arrives. Opening
        # PipeWire is not instantaneous, so "recording started" and "the
        # microphone is listening" are not the same moment -- the start cue must
        # use the latter, or the user speaks into a stream that is not up yet.
        self.on_ready = on_ready
        self.keep_warm_seconds = keep_warm_seconds

        # Everything below the lock is shared with the streaming thread.
        self._lock = threading.Lock()
        self._chunks: list[bytes] = []
        self._capturing = False
        self._ready_fired = False
        self._latest: bytes | None = None

        self._pipeline: Gst.Pipeline | None = None
        self._sink = None
        self._bus = None
        self._warm_timeout: int | None = None
        self._level_timeout: int | None = None
        self._device: str = ""
        self.last_error: str | None = None

    # -- pipeline lifecycle ----------------------------------------------

    def _build(self, device: str) -> bool:
        """Prefer native PipeWire; fall back to whatever the host offers."""
        attempts = []
        if device:
            attempts.append(f'pipewiresrc target-object="{device}"')
            attempts.append(f'pulsesrc device="{device}"')
        attempts += ["pipewiresrc", "autoaudiosrc"]

        for src in attempts:
            desc = (
                f"{src} ! audioconvert ! audioresample ! {CAPS} ! "
                f"appsink name=sink emit-signals=true max-buffers=64 "
                f"drop=false sync=false"
            )
            try:
                pipeline = Gst.parse_launch(desc)
            except GLib.Error as exc:
                log.debug("pipeline %r rejected: %s", src, exc.message)
                continue
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(Gst.State.NULL)
                log.debug("pipeline %r failed to start", src)
                continue
            self._pipeline = pipeline
            self._sink = pipeline.get_by_name("sink")
            self._sink.connect("new-sample", self._on_sample)
            self._bus = pipeline.get_bus()
            self._bus.add_signal_watch()
            self._bus.connect("message::error", self._on_bus_error)
            self._start_level_timer()
            log.info("capturing with %s", src)
            return True

        self.last_error = "no usable audio source; is microphone access granted?"
        log.error(self.last_error)
        return False

    def _teardown(self) -> None:
        self._cancel_warm_timer()
        self._stop_level_timer()
        if self._bus is not None:
            self._bus.remove_signal_watch()
            self._bus = None
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._sink = None
        with self._lock:
            self._latest = None

    def _cancel_warm_timer(self) -> None:
        if self._warm_timeout is not None:
            GLib.source_remove(self._warm_timeout)
            self._warm_timeout = None

    def _on_warm_timeout(self) -> bool:
        # The source is being dispatched, so it must not be removed by hand:
        # forget its id before tearing down.
        self._warm_timeout = None
        self._teardown()
        return GLib.SOURCE_REMOVE

    def _start_level_timer(self) -> None:
        if self._level_timeout is None and self._on_level is not None:
            self._level_timeout = GLib.timeout_add(LEVEL_INTERVAL_MS, self._on_level_tick)

    def _stop_level_timer(self) -> None:
        if self._level_timeout is not None:
            GLib.source_remove(self._level_timeout)
            self._level_timeout = None

    # -- callbacks -------------------------------------------------------

    def _on_bus_error(self, _bus, message) -> None:
        err, debug = message.parse_error()
        self.last_error = err.message
        log.error("audio pipeline error: %s (%s)", err.message, debug)

    def _on_sample(self, sink) -> int:
        """Streaming thread: copy the buffer out and nothing else."""
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            data = bytes(info.data)
        finally:
            buf.unmap(info)
        self._ingest(data)
        return Gst.FlowReturn.OK

    def _ingest(self, data: bytes) -> None:
        """Thread-safe core of _on_sample; anything main-loop-only is deferred."""
        fire_ready = False
        with self._lock:
            self._latest = data
            if self._capturing:
                self._chunks.append(data)
                if not self._ready_fired:
                    self._ready_fired = True
                    fire_ready = True
        if fire_ready:
            GLib.idle_add(self._fire_ready)

    def _fire_ready(self) -> bool:
        # Main loop. The recording may have ended in the meantime; the cue is
        # only meaningful while it is still running.
        if self._capturing and self.on_ready:
            self.on_ready()
        return GLib.SOURCE_REMOVE

    def _on_level_tick(self) -> bool:
        with self._lock:
            data, self._latest = self._latest, None
        if data is not None and self._on_level:
            self._on_level(_rms(data))
        return GLib.SOURCE_CONTINUE

    # -- public API ------------------------------------------------------

    @property
    def is_recording(self) -> bool:
        return self._capturing

    @property
    def duration_ms(self) -> int:
        with self._lock:
            frames = sum(len(c) for c in self._chunks) // 4
        return int(frames * 1000 / SAMPLE_RATE)

    def warm_up(self, device: str = "") -> bool:
        """Open the microphone ahead of time so the first word is not clipped."""
        self._cancel_warm_timer()
        if self._pipeline is not None and device == self._device:
            return True
        self._teardown()
        self._device = device
        return self._build(device)

    def start(self, device: str = "") -> bool:
        if not self.warm_up(device):
            return False
        with self._lock:
            self._chunks = []
            self._ready_fired = False
            self._capturing = True
        return True

    def stop(self) -> bytes:
        """Stop capturing and return the raw float32 PCM."""
        with self._lock:
            self._capturing = False
            chunks, self._chunks = self._chunks, []
        data = b"".join(chunks)
        if self.keep_warm_seconds > 0:
            self._cancel_warm_timer()
            self._warm_timeout = GLib.timeout_add_seconds(
                self.keep_warm_seconds, self._on_warm_timeout
            )
        else:
            self._teardown()
        return data

    def cancel(self) -> None:
        with self._lock:
            self._capturing = False
            self._chunks = []
        if self.keep_warm_seconds <= 0:
            self._teardown()

    def shutdown(self) -> None:
        self.cancel()
        self._teardown()


def _rms(data: bytes) -> float:
    """Perceptual level in 0..1 for the UI meter."""
    if len(data) < 4:
        return 0.0
    samples = array.array("f")
    samples.frombytes(data[: len(data) - (len(data) % 4)])
    if not samples:
        return 0.0
    total = math.fsum(s * s for s in samples)
    rms = math.sqrt(total / len(samples))
    # Map roughly -60..0 dBFS onto 0..1 so quiet speech is still visible.
    if rms <= 1e-6:
        return 0.0
    db = 20 * math.log10(min(rms, 1.0))
    return max(0.0, min(1.0, (db + 60.0) / 60.0))
