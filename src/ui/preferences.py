"""Preferences dialog."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, GObject, Gtk

import audio

LANGUAGES = [
    ("auto", "Detect automatically"), ("en", "English"), ("pt", "Portuguese"),
    ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("it", "Italian"),
    ("nl", "Dutch"), ("pl", "Polish"), ("ru", "Russian"), ("uk", "Ukrainian"),
    ("tr", "Turkish"), ("ar", "Arabic"), ("hi", "Hindi"), ("zh", "Chinese"),
    ("ja", "Japanese"), ("ko", "Korean"),
]

# Custom words are edited a keystroke at a time; settling before writing keeps
# one GSettings write per pause rather than one per letter.
WORDS_SETTLE_MS = 500

UNLOAD_CHOICES = [
    (0, "Never"), (15, "After 15 seconds"), (120, "After 2 minutes"),
    (300, "After 5 minutes"), (900, "After 15 minutes"), (3600, "After 1 hour"),
]


def _combo(title, subtitle, choices, current, on_change):
    """A ComboRow over (value, label) pairs, returning values not indices."""
    model = Gtk.StringList()
    for _value, label in choices:
        model.append(label)
    row = Adw.ComboRow(title=title, subtitle=subtitle, model=model)
    values = [v for v, _ in choices]
    row.set_selected(values.index(current) if current in values else 0)
    row.connect("notify::selected",
                lambda r, _p: on_change(values[r.get_selected()]))
    return row


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, application) -> None:
        super().__init__()
        self.app = application
        self.settings = application.settings
        self._words_timer: int | None = None

        self.add(self._general_page())
        self.add(self._audio_page())
        self.add(self._text_page())

        # Closing the dialog mid-word must not lose the last edit.
        self.connect("closed", lambda *_: self._flush_words())

    # -- General ---------------------------------------------------------

    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        s = self.settings

        activation = Adw.PreferencesGroup(title="Activation")

        mode = _combo(
            "Mode",
            "Hold the shortcut while speaking, or press once to start and again to stop",
            [("push-to-talk", "Hold to talk"), ("toggle", "Press to start and stop")],
            s.get_string("activation-mode"),
            lambda v: s.set_string("activation-mode", v),
        )
        activation.add(mode)

        shortcut = Adw.ActionRow(
            title="Keyboard shortcut",
            subtitle=self._trigger_text(),
        )
        button = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self.app.activate_action("configure-shortcuts", None))
        shortcut.add_suffix(button)
        activation.add(shortcut)

        limit = Adw.SpinRow.new_with_range(5, 600, 5)
        limit.set_title("Recording limit")
        limit.set_subtitle("Stop automatically after this many seconds")
        s.bind("max-recording-seconds", limit, "value", Gio.SettingsBindFlags.DEFAULT)
        activation.add(limit)

        tail = Adw.SpinRow.new_with_range(0, 2000, 50)
        tail.set_title("Trailing buffer")
        tail.set_subtitle("Keep recording briefly after you release, in milliseconds")
        s.bind("extra-buffer-ms", tail, "value", Gio.SettingsBindFlags.DEFAULT)
        activation.add(tail)
        page.add(activation)

        output = Adw.PreferencesGroup(
            title="Output",
            description="Direct typing is unavailable on GNOME: the compositor "
                        "drops characters that are not on your keyboard layout, "
                        "so Scribe pastes instead.",
        )
        output.add(_combo(
            "Insert text by", "How the transcript reaches the focused app",
            [("paste", "Pasting automatically"), ("clipboard", "Copying, so you can paste")],
            s.get_string("output-mode"),
            lambda v: s.set_string("output-mode", v),
        ))
        escalate = Adw.SwitchRow(
            title="Find the right paste shortcut",
            subtitle="Different apps paste with different keys, so Scribe tries "
                     "them in turn until the text lands",
        )
        s.bind("paste-escalate", escalate, "active", Gio.SettingsBindFlags.DEFAULT)
        output.add(escalate)

        chord = _combo(
            "Paste shortcut", "Used only when Scribe is not trying them in turn",
            [("ctrl-v", "Ctrl+V"), ("ctrl-shift-v", "Ctrl+Shift+V"),
             ("shift-insert", "Shift+Insert"), ("paste-key", "Paste key")],
            s.get_string("paste-chord"),
            lambda v: s.set_string("paste-chord", v),
        )
        escalate.bind_property(
            "active", chord, "sensitive",
            GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.INVERT_BOOLEAN,
        )
        output.add(chord)

        restore = Adw.SwitchRow(
            title="Restore clipboard",
            subtitle="Put back what was on the clipboard after pasting",
        )
        s.bind("restore-clipboard", restore, "active", Gio.SettingsBindFlags.DEFAULT)
        output.add(restore)

        space = Adw.SwitchRow(title="Add a trailing space")
        s.bind("append-trailing-space", space, "active", Gio.SettingsBindFlags.DEFAULT)
        output.add(space)
        page.add(output)

        system = Adw.PreferencesGroup(title="System")
        autostart = Adw.SwitchRow(
            title="Start at login",
            subtitle="Scribe keeps listening for the shortcut in the background",
        )
        s.bind("autostart", autostart, "active", Gio.SettingsBindFlags.DEFAULT)
        autostart.connect("notify::active", lambda *_: self.app._request_background())
        system.add(autostart)

        hidden = Adw.SwitchRow(title="Start without a window")
        s.bind("start-hidden", hidden, "active", Gio.SettingsBindFlags.DEFAULT)
        system.add(hidden)

        page.add(system)

        privacy = Adw.PreferencesGroup(
            title="History",
            description="Your speech is transcribed on this computer and is "
                        "never uploaded. Recordings are discarded as soon as "
                        "they have been transcribed.",
        )
        keep_history = Adw.SwitchRow(
            title="Keep recent transcripts",
            subtitle="Turning this off erases what is already stored",
        )
        s.bind("history-enabled", keep_history, "active", Gio.SettingsBindFlags.DEFAULT)
        privacy.add(keep_history)

        limit = Adw.SpinRow.new_with_range(0, 100, 1)
        limit.set_title("How many to keep")
        limit.set_subtitle("Anything older is erased from this computer")
        s.bind("history-limit", limit, "value", Gio.SettingsBindFlags.DEFAULT)
        s.bind("history-enabled", limit, "sensitive", Gio.SettingsBindFlags.GET)
        privacy.add(limit)

        clear = Adw.ActionRow(
            title="Clear history now",
            subtitle="Erase every stored transcript from this computer",
        )
        clear_button = Gtk.Button(label="Clear", valign=Gtk.Align.CENTER)
        clear_button.add_css_class("destructive-action")
        clear_button.connect("clicked", lambda *_: self._confirm_clear())
        clear.add_suffix(clear_button)
        privacy.add(clear)
        page.add(privacy)
        return page

    def _confirm_clear(self) -> None:
        dialog = Adw.AlertDialog(
            heading="Clear history?",
            body="Every stored transcript will be erased from this computer. "
                 "This cannot be undone.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", "Clear")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def respond(_d, response: str) -> None:
            if response != "clear":
                return
            self.app.history.clear()
            if self.app.window:
                self.app.window.history_page.reload()

        dialog.connect("response", respond)
        dialog.present(self)

    def _trigger_text(self) -> str:
        if self.app.shortcut_error:
            return "Could not be registered"
        if self.app.shortcuts and self.app.shortcuts.triggers:
            return self.app.shortcuts.triggers.get("dictate", "Not set")
        return "Not set"

    # -- Audio and model -------------------------------------------------

    def _audio_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Audio", icon_name="audio-input-microphone-symbolic")
        s = self.settings

        mic = Adw.PreferencesGroup(title="Microphone")
        devices = audio.list_devices()
        mic.add(_combo(
            "Input device", "Which microphone to record from",
            devices, s.get_string("input-device"),
            lambda v: s.set_string("input-device", v),
        ))

        warm = Adw.SpinRow.new_with_range(0, 300, 5)
        warm.set_title("Keep microphone open")
        warm.set_subtitle("Seconds to stay ready after dictating, for faster starts")
        s.bind("keep-stream-open-seconds", warm, "value", Gio.SettingsBindFlags.DEFAULT)
        mic.add(warm)

        cue = Adw.SwitchRow(
            title="Play a sound",
            subtitle="A short chime when recording starts and stops",
        )
        s.bind("sound-feedback", cue, "active", Gio.SettingsBindFlags.DEFAULT)
        mic.add(cue)

        volume = Adw.SpinRow.new_with_range(0.0, 1.0, 0.1)
        volume.set_title("Sound volume")
        s.bind("sound-volume", volume, "value", Gio.SettingsBindFlags.DEFAULT)
        mic.add(volume)
        page.add(mic)

        recog = Adw.PreferencesGroup(title="Recognition")
        recog.add(_combo(
            "Language", "Choosing a language is more accurate than detecting it",
            LANGUAGES, s.get_string("language"),
            lambda v: s.set_string("language", v),
        ))

        translate = Adw.SwitchRow(
            title="Translate to English",
            subtitle="Transcribe speech in any language as English",
        )
        s.bind("translate-to-english", translate, "active", Gio.SettingsBindFlags.DEFAULT)
        recog.add(translate)

        recog.add(_combo(
            "Run on", "GPU acceleration uses Vulkan when available",
            [("auto", "Automatic"), ("gpu", "Graphics card"), ("cpu", "Processor")],
            s.get_string("accelerator"),
            lambda v: s.set_string("accelerator", v),
        ))
        recog.add(_combo(
            "Release model from memory", "Frees memory when you stop dictating",
            UNLOAD_CHOICES, s.get_int("model-unload-seconds"),
            lambda v: s.set_int("model-unload-seconds", v),
        ))
        page.add(recog)

        vad = Adw.PreferencesGroup(
            title="Silence detection",
            description="Trimming silence makes transcription faster and stops the "
                        "model inventing words during pauses.",
        )
        # Only the on/off switch is offered: the installed pywhispercpp exposes
        # `vad` and `vad_model_path` but not the threshold/padding knobs, and a
        # control that silently does nothing is worse than no control.
        vad_on = Adw.SwitchRow(title="Trim silence")
        s.bind("vad-enabled", vad_on, "active", Gio.SettingsBindFlags.DEFAULT)
        vad.add(vad_on)
        page.add(vad)
        return page

    # -- Text ------------------------------------------------------------

    def _text_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Text", icon_name="format-text-rich-symbolic")
        s = self.settings

        tidy = Adw.PreferencesGroup(title="Tidying")
        fillers = Adw.SwitchRow(
            title="Remove filler words",
            subtitle="Drops standalone “um”, “uh” and similar",
        )
        s.bind("remove-filler-words", fillers, "active", Gio.SettingsBindFlags.DEFAULT)
        tidy.add(fillers)

        caps = Adw.SwitchRow(title="Capitalise the first word")
        s.bind("capitalize-first", caps, "active", Gio.SettingsBindFlags.DEFAULT)
        tidy.add(caps)
        page.add(tidy)

        vocab = Adw.PreferencesGroup(
            title="Custom words",
            description="Names and jargon that transcription should be nudged "
                        "towards. One per line.",
        )
        self.words_view = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD, top_margin=8, bottom_margin=8,
            left_margin=8, right_margin=8, height_request=140,
        )
        self.words_view.get_buffer().set_text("\n".join(s.get_strv("custom-words")))
        self.words_view.get_buffer().connect("changed", self._save_words)
        frame = Gtk.Frame()
        frame.set_child(self.words_view)
        vocab.add(frame)
        page.add(vocab)
        return page

    def _save_words(self, _buffer: Gtk.TextBuffer) -> None:
        self._cancel_words_timer()
        self._words_timer = GLib.timeout_add(WORDS_SETTLE_MS, self._flush_words)

    def _cancel_words_timer(self) -> None:
        if self._words_timer is not None:
            GLib.source_remove(self._words_timer)
            self._words_timer = None

    def _flush_words(self) -> bool:
        self._cancel_words_timer()
        buffer = self.words_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        words = [line.strip() for line in text.splitlines() if line.strip()]
        if words != list(self.settings.get_strv("custom-words")):
            self.settings.set_strv("custom-words", words)
        return GLib.SOURCE_REMOVE
