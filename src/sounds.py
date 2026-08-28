"""Short audio cues.

GNOME gives us no way to draw a recording overlay (mutter implements no
layer-shell, and any normal window would steal focus and break paste-back), so
sound is the primary feedback channel. GTK's MediaFile plays these through the
GStreamer already in the runtime, so this costs no dependency.
"""

from __future__ import annotations

import logging
import os

from gi.repository import Gtk

log = logging.getLogger(__name__)

START, STOP, ERROR = "start", "stop", "error"


class SoundPlayer:
    def __init__(self, sound_dir: str) -> None:
        self.sound_dir = sound_dir
        self.enabled = True
        self.volume = 0.6
        # Keep each stream alive; a GC'd MediaFile stops mid-playback.
        self._streams: dict[str, Gtk.MediaFile] = {}

    def _stream(self, name: str) -> Gtk.MediaFile | None:
        if name in self._streams:
            return self._streams[name]
        path = os.path.join(self.sound_dir, f"{name}.ogg")
        if not os.path.exists(path):
            log.debug("sound %s missing at %s", name, path)
            return None
        stream = Gtk.MediaFile.new_for_filename(path)
        self._streams[name] = stream
        return stream

    def play(self, name: str) -> None:
        if not self.enabled:
            return
        stream = self._stream(name)
        if stream is None:
            return
        try:
            stream.set_volume(self.volume)
            stream.seek(0)
            stream.play()
        except Exception as exc:  # pragma: no cover - depends on codecs
            log.debug("could not play %s: %s", name, exc)
