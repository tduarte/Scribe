"""Browse, download and choose speech models."""

from __future__ import annotations

from gi.repository import Adw, Gio, GLib, Gtk

from models import Download
from relative_time import remaining

# How long a download runs before its rate is worth an estimate.
ETA_AFTER_SECONDS = 3

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
        self._progress: dict[str, tuple[Adw.ActionRow, Gtk.ProgressBar]] = {}
        # The last status and fraction shown, so a rebuilt row picks up where
        # the old one left off instead of waiting for the next chunk.
        self._status: dict[str, tuple[str, float]] = {}
        self._syncing = False

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

        # Rebuilt on the next main-loop pass: the change usually arrives from a
        # toggle on a row this is about to destroy.
        self.settings.connect(
            "changed::active-model",
            lambda *_: GLib.idle_add(self._refresh_idle),
        )
        self.refresh()

    def _refresh_idle(self) -> bool:
        self.refresh()
        return GLib.SOURCE_REMOVE

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
        # Choosing the active model is an exclusive choice over a list, which
        # the HIG models with radio rows rather than a button on every row and
        # a status label on the chosen one.
        self._syncing = True
        leader: Gtk.CheckButton | None = None
        for model in installed:
            row, check = self._installed_row(model, model.id == active, leader)
            leader = leader or check
            self._installed_group.add(row)
        self._syncing = False
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

    def _installed_row(self, model, is_active: bool,
                       leader: Gtk.CheckButton | None):
        row = Adw.ActionRow(title=model.name, subtitle=self._subtitle(model))

        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        if leader is not None:
            check.set_group(leader)
        check.set_active(is_active)
        check.connect("toggled", self._on_model_toggled, model)
        row.add_prefix(check)
        # Activating anywhere on the row picks the model, so the whole row is
        # the target rather than a small control at its edge.
        row.set_activatable_widget(check)

        menu = Gio.Menu()
        menu.append("Remove", f"models.remove::{model.id}")
        row.add_suffix(Gtk.MenuButton(
            icon_name="view-more-symbolic", menu_model=menu,
            valign=Gtk.Align.CENTER, css_classes=["flat"],
            tooltip_text="More options",
        ))
        return row, check

    def _on_model_toggled(self, check: Gtk.CheckButton, model) -> None:
        # Ignore the toggles emitted while the list is being rebuilt.
        if self._syncing or not check.get_active():
            return
        if self.settings.get_string("active-model") != model.id:
            self.settings.set_string("active-model", model.id)

    def _available_row(self, model) -> Adw.ActionRow:
        row = Adw.ActionRow(title=model.name, subtitle=self._subtitle(model))
        downloading = model.id in self._downloads

        if downloading:
            # The subtitle carries the status while the download runs, so the
            # bar needs no text of its own. Tabular figures keep the line from
            # shifting as the digits change.
            row.add_css_class("numeric")
            status, fraction = self._status.get(
                model.id, ("Starting download…", 0.0)
            )
            row.set_subtitle(status)
            bar = Gtk.ProgressBar(
                valign=Gtk.Align.CENTER, width_request=160, fraction=fraction
            )
            bar.update_property(
                [Gtk.AccessibleProperty.LABEL], [f"Downloading {model.name}"]
            )
            self._progress[model.id] = (row, bar)
            row.add_suffix(bar)
            stop = Gtk.Button(
                icon_name="process-stop-symbolic", valign=Gtk.Align.CENTER,
                css_classes=["flat", "circular"], tooltip_text="Cancel download",
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

    def download(self, model) -> None:
        if model.id in self._downloads:
            return

        started = GLib.get_monotonic_time()

        def progress(received: int, total: int) -> None:
            entry = self._progress.get(model.id)
            if entry is None:
                return
            row, bar = entry
            if not total:
                bar.pulse()
                status = f"{GLib.format_size(received)} downloaded"
                self._status[model.id] = (status, 0.0)
                row.set_subtitle(status)
                return
            fraction = min(1.0, received / total)
            bar.set_fraction(fraction)
            status = f"{GLib.format_size(received)} of {GLib.format_size(total)}"
            # The rate over the first few seconds says little about the rest.
            elapsed = (GLib.get_monotonic_time() - started) / 1_000_000
            if elapsed >= ETA_AFTER_SECONDS and received:
                status += f" · {remaining((total - received) * elapsed / received)}"
            self._status[model.id] = (status, fraction)
            row.set_subtitle(status)

        def done(ok: bool, error: str) -> None:
            self._downloads.pop(model.id, None)
            self._progress.pop(model.id, None)
            self._status.pop(model.id, None)
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
