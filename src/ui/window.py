"""Main window: dictation status, models, and history."""

from __future__ import annotations

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from dictation import State
from ui.history_page import HistoryPage
from ui.models_page import ModelsPage

STATE_TITLE = {
    State.IDLE: "Ready",
    State.RECORDING: "Listening…",
    State.TRANSCRIBING: "Transcribing…",
    State.DELIVERING: "Inserting…",
}

STATE_ICON = {
    State.IDLE: "microphone-sensitivity-muted-symbolic",
    State.RECORDING: "microphone-sensitivity-high-symbolic",
    State.TRANSCRIBING: "content-loading-symbolic",
    State.DELIVERING: "edit-paste-symbolic",
}


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
        header.set_title_widget(Adw.ViewSwitcher(
            stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE
        ))

        menu = Gio.Menu()
        menu.append("Keyboard Shortcut…", "app.configure-shortcuts")
        menu.append("Preferences", "app.preferences")
        menu.append("About Scribe", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(
            icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu"
        ))

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        toolbar.add_bottom_bar(Adw.ViewSwitcherBar(stack=self.stack, reveal=True))

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
    """The at-a-glance view: what the shortcut is, and what was last said."""

    def __init__(self, application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = application

        self.status = Adw.StatusPage(
            icon_name=STATE_ICON[State.IDLE],
            title="Ready",
            description="Hold your shortcut anywhere and speak.",
            vexpand=True,
        )

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            halign=Gtk.Align.CENTER, width_request=380,
        )

        self.level = Gtk.LevelBar(
            min_value=0.0, max_value=1.0, value=0.0,
            mode=Gtk.LevelBarMode.CONTINUOUS, height_request=8,
        )
        self.level.set_visible(False)
        content.append(self.level)

        self.record_button = Gtk.Button(
            label="Start Dictating", halign=Gtk.Align.CENTER,
        )
        self.record_button.add_css_class("suggested-action")
        self.record_button.add_css_class("pill")
        self.record_button.connect("clicked", self._on_record_clicked)
        content.append(self.record_button)

        self.transcript = Gtk.Label(
            label="", wrap=True, selectable=True, justify=Gtk.Justification.CENTER,
            xalign=0.5,
        )
        self.transcript.add_css_class("dim-label")
        content.append(self.transcript)

        self.copy_button = Gtk.Button(
            label="Copy Last Transcript", halign=Gtk.Align.CENTER, visible=False,
        )
        self.copy_button.add_css_class("flat")
        self.copy_button.connect("clicked", self._on_copy_clicked)
        content.append(self.copy_button)

        self.status.set_child(content)
        self.append(self.status)

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

    def refresh_shortcut_state(self) -> None:
        app = self.app
        if app.shortcut_error:
            self.status.set_description(
                "The dictation shortcut could not be registered. "
                "You can still dictate from this window."
            )
            return
        trigger = ""
        if app.shortcuts and app.shortcuts.triggers:
            trigger = app.shortcuts.triggers.get("dictate", "")
        if trigger:
            self.status.set_description(f"{trigger} anywhere, then speak.")
        else:
            self.status.set_description("Hold your shortcut anywhere and speak.")

    def on_state(self, state: State, detail: str) -> None:
        self.status.set_title(STATE_TITLE.get(state, "Ready"))
        self.status.set_icon_name(STATE_ICON.get(state, STATE_ICON[State.IDLE]))
        recording = state is State.RECORDING
        self.level.set_visible(recording)
        if not recording:
            self.level.set_value(0.0)

        self.record_button.set_label(
            "Stop Dictating" if recording else "Start Dictating"
        )
        self.record_button.set_sensitive(state in (State.IDLE, State.RECORDING))

        if state is State.IDLE:
            self.refresh_shortcut_state()
            text = self.app.controller.last_text
            if text:
                self.transcript.set_label(text)
                self.copy_button.set_visible(True)

    def set_partial(self, text: str) -> None:
        self.transcript.set_label(text)

    def set_level(self, level: float) -> None:
        if self.level.get_visible():
            self.level.set_value(level)
