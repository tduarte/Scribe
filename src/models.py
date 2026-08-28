"""The Whisper model catalog: what is available, what is on disk, downloading.

Models are not bundled -- they are far too large -- so Scribe ships a catalog
with authoritative sizes and sha256 sums (generated from the Hugging Face API)
and fetches on demand into the app's own data directory. The VAD model is the
exception: at 885 KB it is bundled, so silence trimming works before any speech
model has been downloaded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Callable

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

CHUNK = 1 << 16


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    filename: str
    url: str
    sha256: str
    size: int
    languages: int
    tier: int
    translate: bool

    @property
    def multilingual(self) -> bool:
        return self.languages > 1

    @property
    def size_label(self) -> str:
        return GLib.format_size(self.size)


def human_error(exc: BaseException) -> str:
    return getattr(exc, "message", None) or str(exc)


class ModelStore:
    """Knows the catalog, what is downloaded, and how to fetch the rest."""

    def __init__(self, catalog_path: str, data_dir: str | None = None) -> None:
        with open(catalog_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.catalog_version: int = raw.get("catalog_version", 1)
        self.default_model_id: str = raw.get("default_model", "")
        self._vad = raw.get("vad", {})
        self.models: list[Model] = [Model(**m) for m in raw["models"]]
        self._by_id = {m.id: m for m in self.models}

        self.data_dir = data_dir or os.path.join(GLib.get_user_data_dir(), "models")
        os.makedirs(self.data_dir, exist_ok=True)

    # -- lookup ----------------------------------------------------------

    def get(self, model_id: str) -> Model | None:
        return self._by_id.get(model_id)

    def path_for(self, model: Model) -> str:
        return os.path.join(self.data_dir, model.filename)

    def is_downloaded(self, model: Model) -> bool:
        path = self.path_for(model)
        try:
            # A truncated file from an interrupted download must not count.
            return os.path.getsize(path) == model.size
        except OSError:
            return False

    def downloaded(self) -> list[Model]:
        return [m for m in self.models if self.is_downloaded(m)]

    def delete(self, model: Model) -> None:
        try:
            os.remove(self.path_for(model))
        except FileNotFoundError:
            pass

    def vad_path(self) -> str | None:
        """The bundled Silero VAD model, if it was installed alongside us."""
        name = self._vad.get("filename")
        if not name:
            return None
        for base in GLib.get_system_data_dirs():
            candidate = os.path.join(base, "scribe", "models", name)
            if os.path.exists(candidate):
                return candidate
        local = os.path.join(self.data_dir, name)
        return local if os.path.exists(local) else None

    # -- verification ----------------------------------------------------

    def verify(
        self, model: Model, progress: Callable[[float], None] | None = None
    ) -> bool:
        """Re-hash a downloaded file. Slow; only for explicit user action."""
        path = self.path_for(model)
        digest = hashlib.sha256()
        try:
            total = os.path.getsize(path)
            done = 0
            with open(path, "rb") as fh:
                while chunk := fh.read(CHUNK):
                    digest.update(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(done / total)
        except OSError:
            return False
        return digest.hexdigest() == model.sha256


class Download:
    """A single in-flight model download.

    Streams to a .part file, hashing as it goes, and only renames into place once
    the sha256 matches -- so an interrupted or corrupted download can never be
    mistaken for a usable model.
    """

    def __init__(
        self,
        store: ModelStore,
        model: Model,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        on_done: Callable[[bool, str], None] | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self._on_progress = on_progress
        self._on_done = on_done
        self._cancellable = Gio.Cancellable()
        self._digest = hashlib.sha256()
        self._received = 0
        self._out: Gio.OutputStream | None = None
        self._stream: Gio.InputStream | None = None
        self.part_path = store.path_for(model) + ".part"

    def cancel(self) -> None:
        self._cancellable.cancel()

    def start(self) -> None:
        # libsoup3 is in the GNOME runtime, so no Python HTTP dependency.
        import gi
        gi.require_version("Soup", "3.0")
        from gi.repository import Soup

        try:
            file = Gio.File.new_for_path(self.part_path)
            self._out = file.replace(None, False, Gio.FileCreateFlags.NONE, None)
        except GLib.Error as exc:
            self._finish(False, human_error(exc))
            return

        session = Soup.Session()
        session.set_user_agent("Scribe/0.1")
        self._session = session  # keep alive for the life of the request
        message = Soup.Message.new("GET", self.model.url)
        session.send_async(
            message, GLib.PRIORITY_DEFAULT, self._cancellable,
            lambda s, res: self._on_response(s, res, message),
        )

    def _on_response(self, session, res, message) -> None:
        try:
            self._stream = session.send_finish(res)
        except GLib.Error as exc:
            self._finish(False, human_error(exc))
            return
        status = message.get_status()
        if status != 200:
            self._finish(False, f"server returned HTTP {int(status)}")
            return
        self._read_chunk()

    def _read_chunk(self) -> None:
        assert self._stream is not None
        self._stream.read_bytes_async(
            CHUNK, GLib.PRIORITY_DEFAULT, self._cancellable, self._on_chunk
        )

    def _on_chunk(self, stream, res) -> None:
        try:
            data = stream.read_bytes_finish(res).get_data()
        except GLib.Error as exc:
            self._finish(False, human_error(exc))
            return

        if data:
            try:
                self._out.write_all(data, None)
            except GLib.Error as exc:
                self._finish(False, human_error(exc))
                return
            self._digest.update(data)
            self._received += len(data)
            if self._on_progress:
                self._on_progress(self._received, self.model.size)
            self._read_chunk()
            return

        # End of stream.
        try:
            self._out.close(None)
            self._out = None
            stream.close(None)
        except GLib.Error:
            pass

        if self._digest.hexdigest() != self.model.sha256:
            self._discard()
            self._finish(False, "the downloaded file did not match its checksum")
            return

        try:
            os.replace(self.part_path, self.store.path_for(self.model))
        except OSError as exc:
            self._finish(False, str(exc))
            return
        self._finish(True, "")

    def _discard(self) -> None:
        try:
            os.remove(self.part_path)
        except OSError:
            pass

    def _finish(self, ok: bool, error: str) -> None:
        if self._out is not None:
            try:
                self._out.close(None)
            except GLib.Error:
                pass
            self._out = None
        if not ok and self._cancellable.is_cancelled():
            self._discard()
            error = error or "cancelled"
        if self._on_done:
            self._on_done(ok, error)
