"""First-run setup: explain and request the permissions, then fetch a model.

Each of GNOME's two consent dialogs gets a page of its own, and the dialog is
only requested from that page's button, so nothing appears on screen before
the user has read what it is for. The paste permission matters most: GNOME
presents it as a Remote Desktop request, which reads like someone trying to
take over the computer unless it has been explained first.

No model ships with Scribe -- they are hundreds of megabytes -- so the first run
also has to fetch one.
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
        self.stack.add_named(self._shortcut_page(), "shortcut")
        self.stack.add_named(self._paste_page(), "paste")
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
                "Next, GNOME will ask for two permissions. Scribe explains each "
                "one before it is requested."
            ),
        )
        button = Gtk.Button(label="Continue", halign=Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.connect("clicked", lambda *_: self.stack.set_visible_child_name("shortcut"))
        status.set_child(button)
        return status

    def _shortcut_page(self) -> Gtk.Widget:
        self.shortcut_step = PermissionStep(
            icon_name="preferences-desktop-keyboard-shortcuts-symbolic",
            title="A Shortcut to Hold",
            description=(
                "You dictate by holding a keyboard shortcut while you speak.\n\n"
                "GNOME will open a dialog to add it. Ctrl+Alt+Space is "
                "suggested, and you can pick a different one there or later in "
                "Preferences. Scribe is told only when that shortcut is pressed; "
                "it never sees the rest of your typing."
            ),
            action_label="Set Up Shortcut",
            skip_label="Continue Without Shortcut",
            on_request=self._request_shortcut,
            on_next=lambda: self.stack.set_visible_child_name("paste"),
        )
        return self.shortcut_step

    def _paste_page(self) -> Gtk.Widget:
        self.paste_step = PermissionStep(
            icon_name="edit-paste-symbolic",
            title="Pasting Into Other Apps",
            description=(
                "To put your words where you are typing, Scribe copies them to "
                "the clipboard and presses paste for you.\n\n"
                "GNOME only lets apps press keys through its <b>Remote "
                "Desktop</b> permission, so the dialog will look like a remote "
                "access request and ask to allow remote interaction and "
                "clipboard access. Both are needed.\n\n"
                "Nobody is connecting to your computer, and Scribe cannot see "
                "your screen. GNOME asks only once."
            ),
            action_label="Set Up Pasting",
            skip_label="Continue Without Pasting",
            on_request=self._request_paste,
            on_next=lambda: self.stack.set_visible_child_name("model"),
        )
        return self.paste_step

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

    def _request_shortcut(self) -> None:
        def done(error) -> None:
            if error is None:
                self.shortcut_step.succeeded()
            elif error.cancelled:
                self.shortcut_step.failed(
                    "No shortcut was added. You can still dictate from Scribe's "
                    "window, or try again."
                )
            else:
                self.shortcut_step.failed(
                    "GNOME could not add the shortcut: "
                    + GLib.markup_escape_text(str(error))
                )

        self.app.request_shortcut(done)

    def _request_paste(self) -> None:
        def done(ok: bool) -> None:
            if ok and self.app.injector.clipboard_enabled:
                self.paste_step.succeeded()
                return
            if ok:
                self.paste_step.failed(
                    "Clipboard access was not allowed, and Scribe needs it to "
                    "paste. Try again and leave “Allow Clipboard Access” on."
                )
                return
            self.paste_step.failed(
                "Pasting into other apps was not allowed. Scribe will still "
                "transcribe, and you can copy the text from its window. GNOME "
                "will ask again the next time Scribe tries to paste."
            )

        if self.app.injector.session and not self.app.injector.clipboard_enabled:
            # A retry after clipboard access was refused: the stored grant
            # would be restored as it is, without showing the dialog again.
            self.app.injector.forget_grant()
        self.app.request_paste_permission(done)

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


class PermissionStep(Adw.Bin):
    """One page explaining a GNOME consent dialog, with the button that opens it.

    The request is never made until the user presses the button. If it is
    refused, the page says what that means and offers a retry or a way on.
    """

    def __init__(self, *, icon_name: str, title: str, description: str,
                 action_label: str, skip_label: str, on_request, on_next) -> None:
        super().__init__()
        self._description = description
        self._action_label = action_label
        self._on_request = on_request
        self._on_next = on_next

        self.status = Adw.StatusPage(icon_name=icon_name, title=title,
                                     description=description)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      halign=Gtk.Align.CENTER)

        self.action = Gtk.Button(label=action_label)
        self.action.add_css_class("suggested-action")
        self.action.add_css_class("pill")
        self.action.connect("clicked", lambda *_: self._request())
        box.append(self.action)

        self.skip = Gtk.Button(label=skip_label, visible=False)
        self.skip.add_css_class("flat")
        self.skip.connect("clicked", lambda *_: self._on_next())
        box.append(self.skip)

        self.status.set_child(box)
        self.set_child(self.status)

    def _request(self) -> None:
        self.action.set_sensitive(False)
        self.action.set_label("Waiting for GNOME…")
        self.skip.set_visible(False)
        self._on_request()

    def succeeded(self) -> None:
        self.action.set_sensitive(True)
        self.action.set_label(self._action_label)
        self.status.set_description(self._description)
        self._on_next()

    def failed(self, message: str) -> None:
        self.action.set_sensitive(True)
        self.action.set_label("Try Again")
        self.skip.set_visible(True)
        self.status.set_description(message)
