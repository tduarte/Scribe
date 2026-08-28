"""Browse, download and choose speech models."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk

from models import Download

TIER_LABEL = {
    1: "Fastest, least accurate",
    2: "Fast",
    3: "A good balance",
    4: "More accurate",
    5: "Recommended",
    6: "Most accurate, slowest",
}


class ModelsPage(Gtk.Box):
    def __init__(self, application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = application
        self.settings = application.settings
        self.store = application.models
        self._downloads: dict[str, Download] = {}
        self._progress: dict[str, Gtk.ProgressBar] = {}

        self.page = Adw.PreferencesPage()
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.page)
        self.append(scroller)

        self._installed_group: Adw.PreferencesGroup | None = None
        self._available_group: Adw.PreferencesGroup | None = None

        # Removal lives behind a menu, so it needs an action to target.
        actions = Gio.SimpleActionGroup()
        remove = Gio.SimpleAction.new("remove", GLib.VariantType.new("s"))
        remove.connect("activate", self._on_remove_action)
        actions.add_action(remove)
        self.insert_action_group("models", actions)

        self.settings.connect("changed::active-model", lambda *_: self.refresh())
        self.refresh()

    # -- rendering -------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild both groups.

        Models move between "installed" and "available" as they are downloaded
        and deleted, so rebuilding is simpler and less error-prone than trying
        to reparent individual rows.
        """
        for group in (self._installed_group, self._available_group):
            if group is not None:
                self.page.remove(group)

        active = self.settings.get_string("active-model")
        installed = [m for m in self.store.models if self.store.is_downloaded(m)]
        available = [
            m for m in self.store.models
            if not self.store.is_downloaded(m) or m.id in self._downloads
        ]

        self._installed_group = Adw.PreferencesGroup(
            title="Installed",
            description=(
                "Stored on this computer and used entirely offline."
                if installed else
                "Nothing installed yet. Choose one below to start dictating."
            ),
        )
        for model in installed:
            self._installed_group.add(self._installed_row(model, model.id == active))
        self.page.add(self._installed_group)

        self._available_group = Adw.PreferencesGroup(
            title="Available to download",
            description="Larger models are more accurate but slower to download "
                        "and to run.",
        )
        for model in available:
            self._available_group.add(self._available_row(model))
        self.page.add(self._available_group)

    def _subtitle(self, model) -> str:
        languages = ("English only" if not model.multilingual
                     else f"{model.languages} languages")
        return f"{model.size_label} · {languages} · {TIER_LABEL.get(model.tier, '')}"

    def _installed_row(self, model, is_active: bool) -> Adw.ActionRow:
        row = Adw.ActionRow(title=model.name, subtitle=self._subtitle(model))

        if is_active:
            badge = Gtk.Label(label="In use")
            badge.add_css_class("caption")
            badge.add_css_class("accent")
            badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(badge)
        else:
            use = Gtk.Button(label="Use", valign=Gtk.Align.CENTER)
            use.connect("clicked", lambda *_: self.select(model))
            row.add_suffix(use)

        menu = Gio.Menu()
        menu.append("Remove", f"models.remove::{model.id}")
        row.add_suffix(Gtk.MenuButton(
            icon_name="view-more-symbolic", menu_model=menu,
            valign=Gtk.Align.CENTER, css_classes=["flat"],
            tooltip_text="More options",
        ))
        return row

    def _available_row(self, model) -> Adw.ActionRow:
        row = Adw.ActionRow(title=model.name, subtitle=self._subtitle(model))
        downloading = model.id in self._downloads

        if downloading:
            bar = Gtk.ProgressBar(
                valign=Gtk.Align.CENTER, width_request=140, show_text=True
            )
            self._progress[model.id] = bar
            row.add_suffix(bar)
            stop = Gtk.Button(
                icon_name="process-stop-symbolic", valign=Gtk.Align.CENTER,
                css_classes=["flat"], tooltip_text="Cancel download",
            )
            stop.connect("clicked", lambda *_: self.cancel_download(model))
            row.add_suffix(stop)
        else:
            get = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
            if model.id == self.store.default_model_id:
                get.add_css_class("suggested-action")
            get.connect("clicked", lambda *_: self.download(model))
            row.add_suffix(get)
        return row

    # -- actions ---------------------------------------------------------

    def select(self, model) -> None:
        self.settings.set_string("active-model", model.id)
        self.refresh()

    def download(self, model) -> None:
        if model.id in self._downloads:
            return

        def progress(received: int, total: int) -> None:
            bar = self._progress.get(model.id)
            if bar is not None and total:
                bar.set_fraction(min(1.0, received / total))
                bar.set_text(f"{GLib.format_size(received)} of "
                             f"{GLib.format_size(total)}")

        def done(ok: bool, error: str) -> None:
            self._downloads.pop(model.id, None)
            self._progress.pop(model.id, None)
            if ok:
                if not self.settings.get_string("active-model"):
                    self.settings.set_string("active-model", model.id)
                self.app.notifier.notify(
                    "scribe-model", "Model ready", f"{model.name} is ready to use."
                )
                self._toast(f"{model.name} is ready")
            elif error != "cancelled":
                self._toast(f"Could not download {model.name}: {error}")
            self.refresh()

        dl = Download(self.store, model, on_progress=progress, on_done=done)
        self._downloads[model.id] = dl
        self.refresh()
        dl.start()

    def cancel_download(self, model) -> None:
        dl = self._downloads.get(model.id)
        if dl:
            dl.cancel()

    def _on_remove_action(self, _action, param: GLib.Variant) -> None:
        model = self.store.get(param.get_string())
        if model is not None:
            self._confirm_remove(model)

    def _confirm_remove(self, model) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Remove {model.name}?",
            body=f"The {model.size_label} file will be deleted from this "
                 f"computer. You can download it again later.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, resp: self.remove(model) if resp == "remove" else None,
        )
        dialog.present(self.get_root())

    def remove(self, model) -> None:
        self.store.delete(model)
        if self.settings.get_string("active-model") == model.id:
            remaining = self.store.downloaded()
            self.settings.set_string(
                "active-model", remaining[0].id if remaining else ""
            )
        self._toast(f"Removed {model.name}")
        self.refresh()

    def _toast(self, message: str) -> None:
        window = self.get_root()
        if hasattr(window, "toast"):
            window.toast(message)
