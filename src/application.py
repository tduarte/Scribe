"""Adw.Application: wires the pieces together and owns the app lifecycle.

Scribe is usable with no window open -- the point is to dictate into *other*
applications -- so the process is held alive by g_application_hold() while the
shortcut session is live, and it advertises itself through the Background portal
rather than a tray icon (GNOME has none).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

import audio  # noqa: E402
import sounds  # noqa: E402
from dictation import DictationController, State  # noqa: E402
from history import History  # noqa: E402
from models import ModelStore  # noqa: E402
from portals.background import BackgroundManager  # noqa: E402
from portals.base import PortalError  # noqa: E402
from portals.inject import InjectorState, TextInjector  # noqa: E402
from portals.notify import Notifier  # noqa: E402
from portals.shortcuts import ShortcutManager  # noqa: E402
from transcriber import Transcriber  # noqa: E402

log = logging.getLogger(__name__)

# Binding shortcuts immediately after an autostart launch is unreliable
# (see docs/PORTAL-FINDINGS.md), so give the session a moment to settle.
SHORTCUT_START_DELAY_MS = 3000

STATUS = {
    State.IDLE: "Ready to dictate",
    State.RECORDING: "Recording…",
    State.TRANSCRIBING: "Transcribing…",
    State.DELIVERING: "Inserting text…",
}


class ScribeApplication(Adw.Application):
    def __init__(self, *, version: str, app_id: str, pkgdatadir: str) -> None:
        super().__init__(
            application_id=app_id,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.version = version
        self.pkgdatadir = pkgdatadir
        self.settings = Gio.Settings.new(app_id)

        self.window = None
        self.controller: DictationController | None = None
        self.shortcuts: ShortcutManager | None = None
        self._held = False
        self._start_hidden = False
        self.shortcut_error: str = ""

        self.add_main_option_entries(self._options())
        self._add_actions()

    # -- CLI -------------------------------------------------------------

    @staticmethod
    def _options() -> list[GLib.OptionEntry]:
        def entry(long, short, desc):
            e = GLib.OptionEntry()
            e.long_name = long
            e.short_name = ord(short) if short else 0
            e.flags = 0
            e.arg = GLib.OptionArg.NONE
            e.description = desc
            return e

        return [
            entry("background", "\0", "Start without showing a window"),
            entry("toggle", "\0", "Start or stop dictation, then exit"),
            entry("cancel", "\0", "Cancel dictation in progress, then exit"),
            entry("version", "v", "Show the version and exit"),
        ]

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        opts = command_line.get_options_dict().end().unpack()

        if "version" in opts:
            command_line.print_literal(f"Scribe {self.version}\n")
            return 0
        if "toggle" in opts:
            self.activate_action("toggle", None)
            return 0
        if "cancel" in opts:
            self.activate_action("cancel", None)
            return 0

        self._start_hidden = "background" in opts or self.settings.get_boolean(
            "start-hidden"
        )
        self.activate()
        return 0

    # -- actions ---------------------------------------------------------

    def _add_actions(self) -> None:
        for name, handler in (
            ("toggle", lambda *_: self.controller and self.controller.toggle()),
            ("cancel", lambda *_: self.controller and self.controller.cancel()),
            ("preferences", lambda *_: self._show_preferences()),
            ("about", lambda *_: self._show_about()),
            ("configure-shortcuts", lambda *_: self._configure_shortcuts()),
            ("quit", lambda *_: self.quit()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("app.cancel", ["Escape"])
        self.set_accels_for_action("app.quit", ["<Control>q"])

    # -- lifecycle -------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        audio.init()
        self._load_styles()

        self.models = ModelStore(os.path.join(self.pkgdatadir, "models.json"))
        self.history = History()
        self._enforce_history_policy()
        self.settings.connect("changed::history-limit",
                              lambda *_: self._enforce_history_policy())
        self.settings.connect("changed::history-enabled",
                              lambda *_: self._enforce_history_policy())
        self.settings.connect("changed::history-retention-days",
                              lambda *_: self._enforce_history_policy())

        self.player = sounds.SoundPlayer(os.path.join(self.pkgdatadir, "sounds"))
        self.player.enabled = self.settings.get_boolean("sound-feedback")
        self.player.volume = self.settings.get_double("sound-volume")

        self.notifier = Notifier()
        self.background = BackgroundManager()

        self.injector = TextInjector(
            get_token=lambda: self.settings.get_string("remote-desktop-token"),
            set_token=lambda t: self.settings.set_string("remote-desktop-token", t),
        )

        self.recorder = audio.Recorder(
            on_level=lambda lvl: self._on_level(lvl),
            keep_warm_seconds=self.settings.get_int("keep-stream-open-seconds"),
        )

        worker = [sys.executable, os.path.join(self.pkgdatadir, "scribe", "worker.py")]
        self.transcriber = Transcriber(
            worker,
            on_segment=lambda t: self.controller.on_segment(t),
            on_result=lambda t, l, ms: self.controller.on_result(t, l, ms),
            on_error=lambda m: self.controller.on_error(m),
        )

        self.controller = DictationController(
            settings=self.settings,
            recorder=self.recorder,
            transcriber=self.transcriber,
            injector=self.injector,
            model_store=self.models,
            history=self.history,
            player=self.player,
            notifier=self.notifier,
            on_state=self._on_state,
            on_partial=self._on_partial,
        )

        self.settings.connect("changed::sound-feedback",
                              lambda s, k: setattr(self.player, "enabled",
                                                   s.get_boolean(k)))
        self.settings.connect("changed::sound-volume",
                              lambda s, k: setattr(self.player, "volume",
                                                   s.get_double(k)))

        GLib.timeout_add(SHORTCUT_START_DELAY_MS, self._start_shortcuts)
        self._request_background()

    def _load_styles(self) -> None:
        css_path = os.path.join(self.pkgdatadir, "style.css")
        if not os.path.exists(css_path):
            return
        provider = Gtk.CssProvider()
        # load_from_path is deprecated since GTK 4.12.
        with open(css_path, encoding="utf-8") as fh:
            provider.load_from_string(fh.read())
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _enforce_history_policy(self) -> None:
        """Apply the retention rules to what is already stored.

        Run at startup and whenever the settings change, so lowering the limit
        or switching history off erases the existing entries rather than just
        hiding them.
        """
        if not self.settings.get_boolean("history-enabled"):
            removed = self.history.enforce_limit(0)
        else:
            self.history.prune(self.settings.get_int("history-retention-days"))
            removed = self.history.enforce_limit(
                self.settings.get_int("history-limit")
            )
        if removed:
            log.info("erased %d stored transcript(s)", removed)
        if self.window:
            self.window.history_page.reload()

    def do_activate(self) -> None:
        if self._start_hidden and self.window is None:
            self._hold()
            self._start_hidden = False
            return
        self._present_window()

    def _present_window(self) -> None:
        from ui.window import ScribeWindow

        if self.window is None:
            self.window = ScribeWindow(application=self)
            self.window.connect("close-request", self._on_window_closed)
        self.window.present()

        if not self.settings.get_boolean("onboarding-completed"):
            from ui.onboarding import OnboardingDialog
            OnboardingDialog(self).present(self.window)

    def _on_window_closed(self, *_args) -> bool:
        # Closing the window must not end dictation: that is the whole point.
        self.window = None
        self._hold()
        return False

    def _hold(self) -> None:
        if not self._held:
            self.hold()
            self._held = True

    def do_shutdown(self) -> None:
        if self.shortcuts:
            self.shortcuts.stop()
        if self.controller:
            self.controller.cancel()
        self.recorder.shutdown()
        self.transcriber.stop()
        self.injector.close()
        self.history.close()
        Adw.Application.do_shutdown(self)

    # -- portals ---------------------------------------------------------

    def _start_shortcuts(self) -> bool:
        self.shortcuts = ShortcutManager(
            on_press=lambda: self.controller.on_shortcut_press(),
            on_release=lambda: self.controller.on_shortcut_release(),
            on_error=self._on_shortcut_error,
        )
        self.shortcuts.on_triggers_changed = self._on_triggers_changed
        self.shortcuts.start()
        return GLib.SOURCE_REMOVE

    def _on_shortcut_error(self, error: PortalError) -> None:
        self.shortcut_error = str(error)
        log.error("global shortcut unavailable: %s", error)
        if self.window:
            self.window.refresh_shortcut_state()

    def _on_triggers_changed(self) -> None:
        self.shortcut_error = ""
        if self.window:
            self.window.refresh_shortcut_state()
        self._update_status()

    def _configure_shortcuts(self) -> None:
        if self.shortcuts:
            self.shortcuts.configure()

    def _request_background(self) -> None:
        self.background.request(
            reason="Scribe listens for the dictation shortcut while you work.",
            autostart=self.settings.get_boolean("autostart"),
            commandline=["scribe", "--background"],
        )

    # -- controller callbacks -------------------------------------------

    def _on_state(self, state: State, detail: str) -> None:
        self._update_status(state)
        if self.window:
            self.window.on_state(state, detail)

    def _on_partial(self, text: str) -> None:
        if self.window:
            self.window.on_partial(text)

    def _on_level(self, level: float) -> None:
        if self.window:
            self.window.on_level(level)

    def _update_status(self, state: State | None = None) -> None:
        state = state or (self.controller.state if self.controller else State.IDLE)
        message = STATUS.get(state, "Ready to dictate")
        if state is State.IDLE and self.shortcuts and self.shortcuts.triggers:
            trigger = self.shortcuts.triggers.get("dictate", "")
            if trigger:
                message = f"Ready — {trigger}"
        self.background.set_status(message)

    # -- dialogs ---------------------------------------------------------

    def _show_preferences(self) -> None:
        from ui.preferences import PreferencesDialog
        PreferencesDialog(self).present(self.window)

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="Scribe",
            application_icon=self.get_application_id(),
            developer_name="Tiago Duarte",
            version=self.version,
            comments="Dictate anywhere with your voice, using Whisper running "
                     "entirely on your own machine.",
            website="https://github.com/tduarte/Scribe",
            issue_url="https://github.com/tduarte/Scribe/issues",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self.window)


def main(*, version: str, app_id: str, pkgdatadir: str) -> int:
    logging.basicConfig(
        level=os.environ.get("SCRIBE_LOG", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    app = ScribeApplication(version=version, app_id=app_id, pkgdatadir=pkgdatadir)
    return app.run(sys.argv)
