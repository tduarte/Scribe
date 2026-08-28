"""Stand-ins for the collaborators DictationController orchestrates."""
import struct


class FakeSettings:
    DEFAULTS = {
        "activation-mode": "push-to-talk",
        "max-recording-seconds": 60,
        "extra-buffer-ms": 0,
        "input-device": "",
        "keep-stream-open-seconds": 30,
        "active-model": "turbo",
        "model-unload-seconds": 0,
        "accelerator": "auto",
        "thread-count": 0,
        "language": "auto",
        "translate-to-english": False,
        "vad-enabled": True,
        "vad-threshold": 0.5,
        "vad-min-silence-ms": 100,
        "vad-speech-pad-ms": 30,
        "output-mode": "paste",
        "paste-chord": "ctrl-v",
        "restore-clipboard": True,
        "append-trailing-space": False,
        "paste-delay-ms": 60,
        "custom-words": [],
        "word-correction-threshold": 0.18,
        "remove-filler-words": True,
        "custom-filler-words": [],
        "capitalize-first": True,
        "history-enabled": True,
        "history-limit": 5,
    }

    def __init__(self, **overrides):
        self._v = dict(self.DEFAULTS)
        self._v.update(overrides)

    def set(self, key, value):
        self._v[key] = value

    def get_string(self, k):  return self._v[k]
    def get_int(self, k):     return self._v[k]
    def get_boolean(self, k): return self._v[k]
    def get_double(self, k):  return self._v[k]
    def get_strv(self, k):    return list(self._v[k])


class FakeRecorder:
    def __init__(self, *, fail=False, seconds=1.0):
        self.fail = fail
        self.seconds = seconds
        self.keep_warm_seconds = 30
        self.started = False
        self.cancelled = 0
        self.last_error = None
        self.on_ready = None

    def deliver_first_buffer(self):
        """Simulate PipeWire finally handing over audio."""
        if self.on_ready:
            self.on_ready()

    def start(self, device=""):
        if self.fail:
            self.last_error = "no microphone"
            return False
        self.started = True
        return True

    def stop(self):
        self.started = False
        frames = int(16000 * self.seconds)
        return b"".join(struct.pack("<f", 0.2) for _ in range(frames))

    def cancel(self):
        self.started = False
        self.cancelled += 1


class FakeTranscriber:
    def __init__(self):
        self.requests = []
        self.accept = True
        self.unloaded = 0

    def transcribe(self, audio, *, model_path, **options):
        if not self.accept:
            return False
        self.requests.append({"audio": audio, "model_path": model_path, **options})
        return True

    def unload(self):
        self.unloaded += 1


class FakeInjector:
    def __init__(self, *, ok=True, error=""):
        self.ok, self.error = ok, error
        self.pasted, self.copied = [], []

    def paste(self, text, *, chord="ctrl-v", restore_clipboard=True,
              delay_ms=60, on_done=None):
        self.pasted.append({"text": text, "chord": chord,
                            "restore": restore_clipboard})
        if on_done:
            on_done(self.ok, self.error)

    def copy_only(self, text, on_done=None):
        self.copied.append(text)
        if on_done:
            on_done(self.ok, self.error)


class FakeModel:
    id = "turbo"
    filename = "ggml-large-v3-turbo-q5_0.bin"


class FakeModels:
    def __init__(self, downloaded=True):
        self._downloaded = downloaded
        self.model = FakeModel()

    def get(self, mid):
        return self.model if mid == "turbo" else None

    def is_downloaded(self, m):
        return self._downloaded

    def path_for(self, m):
        return f"/models/{m.filename}"

    def vad_path(self):
        return "/app/share/scribe/models/ggml-silero-v6.2.0.bin"


class FakeHistory:
    def __init__(self):
        self.entries = []
        self.limits_applied = []

    def add(self, text, *, duration_ms=0, model="", language=""):
        self.entries.append((text, duration_ms, model, language))
        return len(self.entries)

    def enforce_limit(self, limit):
        self.limits_applied.append(limit)
        if limit <= 0:
            removed, self.entries = len(self.entries), []
            return removed
        removed = max(0, len(self.entries) - limit)
        if removed:
            self.entries = self.entries[-limit:]
        return removed


class FakePlayer:
    def __init__(self):
        self.played = []

    def play(self, name):
        self.played.append(name)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, nid, title, body="", priority="normal", icon=None):
        self.sent.append((nid, title, body))

    def withdraw(self, nid):
        pass
