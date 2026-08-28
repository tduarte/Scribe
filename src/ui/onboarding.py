"""First-run setup: pick and download a model, and explain the permissions.

No model ships with Scribe -- they are hundreds of megabytes -- so the first run
has to fetch one. This is also the natural moment to explain why GNOME is about
to ask for a global shortcut and for remote input permission.
"""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from models import Download

# Offered on first run: a small one, the balanced default, and an English-only
# option. The full catalog is a click away on the Models page.
SUGGESTED = ("small", "turbo", "base.en")


class OnboardingDialog(Adw.Dialog):
    def __init__(self, application) -> None:
        super().__init__(title="Welcome to Scribe", content_width=520,
                         content_height=560)
        self.app = application
        self.store = application.models
        self.settings = application.settings
        self._download: Download | None = None
        self.set_can_close(False)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT)
        self.stack.add_named(self._intro_page(), "intro")
        self.stack.add_named(self._model_page(), "model")
        self.stack.add_named(self._progress_page(), "progress")

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_end_title_buttons=False))
        toolbar.set_content(self.stack)
        self.set_child(toolbar)

    # -- pages -----------------------------------------------------------

    def _intro_page(self) -> Gtk.Widget:
        status = Adw.StatusPage(
            icon_name=self.app.get_application_id(),
            title="Dictate anywhere",
            description=(
                "Hold a keyboard shortcut, speak, and Scribe types what you said "
                "into whatever app you are using.\n\n"
                "Everything happens on this computer. Your voice is never sent "
                "anywhere.\n\n"
                "GNOME will ask you twice for permission: once to register the "
                "shortcut, and once to let Scribe paste into other apps. Both are "
                "needed for dictation to work."
            ),
        )
        button = Gtk.Button(label="Continue", halign=Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.connect("clicked", lambda *_: self.stack.set_visible_child_name("model"))
        status.set_child(button)
        return status

    def _model_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Choose a speech model",
            description="This is downloaded once. You can add others later.",
        )

        already = self.store.downloaded()
        for model_id in SUGGESTED:
            model = self.store.get(model_id)
            if model is None:
                continue
            row = Adw.ActionRow(
                title=model.name,
                subtitle=f"{model.size_label} · "
                         + ("English only" if not model.multilingual
                            else f"{model.languages} languages"),
            )
            label = "Use" if self.store.is_downloaded(model) else "Download"
            button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            if model_id == self.store.default_model_id:
                button.add_css_class("suggested-action")
            button.connect("clicked", lambda _b, m=model: self._choose(m))
            row.add_suffix(button)
            group.add(row)

        page.add(group)

        if already:
            skip = Adw.PreferencesGroup()
            row = Adw.ActionRow(
                title="Skip for now",
                subtitle="Use a model you have already downloaded",
            )
            button = Gtk.Button(label="Skip", valign=Gtk.Align.CENTER)
            button.add_css_class("flat")
            button.connect("clicked", lambda *_: self._finish(already[0].id))
            row.add_suffix(button)
            skip.add(row)
            page.add(skip)
        return page

    def _progress_page(self) -> Gtk.Widget:
        self.progress_status = Adw.StatusPage(
            icon_name="folder-download-symbolic",
            title="Downloading",
            description="This happens once.",
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      halign=Gtk.Align.CENTER, width_request=320)
        self.progress = Gtk.ProgressBar(show_text=True)
        box.append(self.progress)

        self.cancel_button = Gtk.Button(label="Cancel", halign=Gtk.Align.CENTER)
        self.cancel_button.add_css_class("flat")
        self.cancel_button.connect("clicked", lambda *_: self._cancel())
        box.append(self.cancel_button)
        self.progress_status.set_child(box)
        return self.progress_status

    # -- actions ---------------------------------------------------------

    def _choose(self, model) -> None:
        if self.store.is_downloaded(model):
            self._finish(model.id)
            return

        self.progress_status.set_title(f"Downloading {model.name}")
        self.progress.set_fraction(0.0)
        self.stack.set_visible_child_name("progress")

        def progress(received: int, total: int) -> None:
            if total:
                self.progress.set_fraction(min(1.0, received / total))
                self.progress.set_text(
                    f"{GLib.format_size(received)} of {GLib.format_size(total)}"
                )

        def done(ok: bool, error: str) -> None:
            self._download = None
            if ok:
                self._finish(model.id)
            else:
                self.progress_status.set_title("Download failed")
                self.progress_status.set_description(error)
                self.cancel_button.set_label("Back")
                self.cancel_button.connect(
                    "clicked", lambda *_: self.stack.set_visible_child_name("model")
                )

        self._download = Download(self.store, model, on_progress=progress, on_done=done)
        self._download.start()

    def _cancel(self) -> None:
        if self._download:
            self._download.cancel()
        self.stack.set_visible_child_name("model")

    def _finish(self, model_id: str) -> None:
        self.settings.set_string("active-model", model_id)
        self.settings.set_boolean("onboarding-completed", True)
        self.set_can_close(True)
        self.close()
        if self.app.window:
            self.app.window.models_page.refresh()
            self.app.window.refresh_shortcut_state()
