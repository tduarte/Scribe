"""Main window: dictation status, models, and history."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk

from dictation import State
from shortcut_label import keycaps
from ui.history_page import HistoryPage
from ui.models_page import ModelsPage

STATE_TITLE = {
    State.IDLE: "Ready",
    State.RECORDING: "Listening",
    State.TRANSCRIBING: "Transcribing",
    State.DELIVERING: "Inserting",
}

STATE_ICON = {
    State.IDLE: "audio-input-microphone-symbolic",
    State.RECORDING: "microphone-sensitivity-high-symbolic",
    State.TRANSCRIBING: "content-loading-symbolic",
    State.DELIVERING: "edit-paste-symbolic",
}

# States where the window is mid-flight and the button should not invite a click.
BUSY = (State.TRANSCRIBING, State.DELIVERING)


class ScribeWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="Scribe")
        self.app = application
        self.settings = application.settings

        self.set_default_size(
            self.settings.get_int("window-width"),
            self.settings.get_int("window-height"),
        )
        if self.settings.get_boolean("window-maximized"):
            self.maximize()
        self.connect("notify::default-width", self._save_size)
        self.connect("notify::default-height", self._save_size)
        self.connect("notify::maximized", self._save_size)

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()

        self.dictate_page = DictationPage(application)
        self.models_page = ModelsPage(application)
        self.history_page = HistoryPage(application)

        self.stack.add_titled_with_icon(
            self.dictate_page, "dictate", "Dictate", "audio-input-microphone-symbolic"
        )
        self.stack.add_titled_with_icon(
            self.models_page, "models", "Models", "folder-download-symbolic"
        )
        self.stack.add_titled_with_icon(
            self.history_page, "history", "History", "document-open-recent-symbolic"
        )

        header = Adw.HeaderBar()
        self.switcher = Adw.ViewSwitcher(
            stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE
        )
        header.set_title_widget(self.switcher)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Change Shortcut…", "app.configure-shortcuts")
        section.append("Preferences", "app.preferences")
        menu.append_section(None, section)
        about = Gio.Menu()
        about.append("About Scribe", "app.about")
        about.append("Quit", "app.quit")
        menu.append_section(None, about)
        header.pack_end(Gtk.MenuButton(
            icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu"
        ))

        # One set of tabs at a time, per the HIG: the switcher lives in the
        # header while the window is wide enough, and moves to a bottom bar only
        # when it is not. Showing both at once duplicates the same navigation.
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack, reveal=False)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        toolbar.add_bottom_bar(self.switcher_bar)

        narrow = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 560sp")
        )
        narrow.add_setter(self.switcher_bar, "reveal", True)
        narrow.add_setter(header, "title-widget",
                          Adw.WindowTitle(title="Scribe"))
        self.add_breakpoint(narrow)

        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self.refresh_shortcut_state()

    def _save_size(self, *_args) -> None:
        if not self.is_maximized():
            self.settings.set_int("window-width", self.get_width())
            self.settings.set_int("window-height", self.get_height())
        self.settings.set_boolean("window-maximized", self.is_maximized())

    # -- updates from the controller -------------------------------------

    def on_state(self, state: State, detail: str) -> None:
        self.dictate_page.on_state(state, detail)
        if state is State.IDLE and detail == "delivered":
            self.history_page.reload()
        quiet = ("delivered", "cancelled", "nothing was said")
        if detail and state is State.IDLE and detail not in quiet:
            self.toast(detail)

    def on_partial(self, text: str) -> None:
        self.dictate_page.set_partial(text)

    def on_level(self, level: float) -> None:
        self.dictate_page.set_level(level)

    def refresh_shortcut_state(self) -> None:
        self.dictate_page.refresh_shortcut_state()

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, timeout=4))


class DictationPage(Gtk.Box):
    """The home view: what to press, what is happening, what was just said."""

    def __init__(self, application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = application
        self.settings = application.settings

        self.status = Adw.StatusPage(
            icon_name=STATE_ICON[State.IDLE], title="Ready", vexpand=True
        )

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=20,
            halign=Gtk.Align.CENTER, width_request=420,
        )

        # The shortcut is the primary interface, so it leads.
        self.shortcut_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
            halign=Gtk.Align.CENTER,
        )
        content.append(self.shortcut_box)

        # Stands in for the recording overlay GNOME cannot host.
        self.level = Gtk.LevelBar(
            min_value=0.0, max_value=1.0, value=0.0,
            mode=Gtk.LevelBarMode.CONTINUOUS,
        )
        self.level.add_css_class("level-meter")
        self.level_revealer = Gtk.Revealer(
            child=self.level, transition_type=Gtk.RevealerTransitionType.CROSSFADE
        )
        content.append(self.level_revealer)

        self.record_button = Gtk.Button(
            label="Start Dictating", halign=Gtk.Align.CENTER
        )
        self.record_button.add_css_class("suggested-action")
        self.record_button.add_css_class("pill")
        self.record_button.connect("clicked", self._on_record_clicked)
        content.append(self.record_button)

        content.append(self._build_transcript_card())

        self.footer = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER)
        self.footer.add_css_class("caption")
        self.footer.add_css_class("dim-label")
        content.append(self.footer)

        self.status.set_child(content)
        self.append(self.status)

        self.settings.connect("changed::active-model", lambda *_: self._update_footer())
        self.settings.connect("changed::accelerator", lambda *_: self._update_footer())
        self._update_footer()

    def _build_transcript_card(self) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("card")
        card.add_css_class("transcript-card")

        self.transcript = Gtk.Label(
            label="", wrap=True, selectable=True, xalign=0.0,
            margin_top=12, margin_start=12, margin_end=12,
        )
        self.transcript.add_css_class("transcript-text")
        card.append(self.transcript)

        actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
            halign=Gtk.Align.END, margin_bottom=6, margin_end=6,
        )
        self.copy_button = Gtk.Button(label="Copy")
        self.copy_button.add_css_class("flat")
        self.copy_button.connect("clicked", self._on_copy_clicked)
        actions.append(self.copy_button)
        card.append(actions)

        self.transcript_revealer = Gtk.Revealer(
            child=card, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN
        )
        return self.transcript_revealer

    # -- interaction -----------------------------------------------------

    def _on_record_clicked(self, _button) -> None:
        self.app.controller.toggle()

    def _on_copy_clicked(self, _button) -> None:
        text = self.app.controller.last_text
        if not text:
            return
        self.get_clipboard().set(text)
        window = self.get_root()
        if isinstance(window, ScribeWindow):
            window.toast("Copied to clipboard")

    # -- display ---------------------------------------------------------

    def _set_shortcut_widgets(self, caps: list[str]) -> None:
        child = self.shortcut_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.shortcut_box.remove(child)
            child = nxt

        if not caps:
            return

        lead = Gtk.Label(label="Hold")
        lead.add_css_class("dim-label")
        self.shortcut_box.append(lead)
        for i, cap in enumerate(caps):
            if i:
                plus = Gtk.Label(label="+")
                plus.add_css_class("dim-label")
                self.shortcut_box.append(plus)
            key = Gtk.Label(label=cap)
            key.add_css_class("keycap")
            self.shortcut_box.append(key)
        tail = Gtk.Label(label="and speak")
        tail.add_css_class("dim-label")
        self.shortcut_box.append(tail)

    def refresh_shortcut_state(self) -> None:
        app = self.app
        trigger = ""
        if not app.shortcut_error and app.shortcuts and app.shortcuts.triggers:
            trigger = app.shortcuts.triggers.get("dictate", "")
        caps = keycaps(trigger)
        self._set_shortcut_widgets(caps)
        self.status.set_description(self._description(caps))

    def _description(self, caps: list[str]) -> str | None:
        """Whatever currently stands between the user and dictating.

        The shortcut comes first: without it there is no dictation to insert.
        """
        if self.app.shortcut_error:
            return ("The dictation shortcut could not be registered. "
                    "You can still dictate from this window.")
        if self.app.injector_error:
            return ("Scribe was not allowed to paste into other applications, "
                    "so dictated text cannot be inserted for you. Copying it "
                    "from here still works.")
        if not caps:
            return "Set a shortcut to dictate from anywhere."
        return None

    def _update_footer(self) -> None:
        model = self.app.models.get(self.settings.get_string("active-model"))
        if model is None:
            self.footer.set_label("No model installed yet")
            return
        accel = self.settings.get_string("accelerator")
        where = {"gpu": "graphics card", "cpu": "processor"}.get(
            accel, "graphics card when available"
        )
        self.footer.set_label(f"{model.name} · running on your {where}")

    def on_state(self, state: State, detail: str) -> None:
        self.status.set_title(STATE_TITLE.get(state, "Ready"))
        self.status.set_icon_name(STATE_ICON.get(state, STATE_ICON[State.IDLE]))

        recording = state is State.RECORDING
        self.level_revealer.set_reveal_child(recording)
        if not recording:
            self.level.set_value(0.0)

        self.record_button.set_label("Stop" if recording else "Start Dictating")
        self.record_button.set_sensitive(state not in BUSY)
        self.shortcut_box.set_sensitive(state is State.IDLE)

        if state is State.IDLE:
            self.refresh_shortcut_state()
            text = self.app.controller.last_text
            if text:
                self.transcript.set_label(text)
                self.transcript_revealer.set_reveal_child(True)

    def set_partial(self, text: str) -> None:
        self.transcript.set_label(text)
        self.transcript_revealer.set_reveal_child(bool(text))

    def set_level(self, level: float) -> None:
        if self.level_revealer.get_reveal_child():
            self.level.set_value(level)
