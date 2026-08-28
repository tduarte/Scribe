"""Browse, download and choose speech models."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from models import Download

TIER_LABEL = {
    1: "Fastest, least accurate",
    2: "Fast",
    3: "Balanced",
    4: "Accurate",
    5: "Recommended",
    6: "Most accurate, slowest",
}


class ModelsPage(Gtk.Box):
    def __init__(self, application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = application
        self.settings = application.settings
        self.store = application.models
        self._rows: dict[str, ModelRow] = {}
        self._downloads: dict[str, Download] = {}

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title="Speech Models",
            description="Models run entirely on your computer. Larger models are "
                        "more accurate but take longer to download and to run.",
        )
        for model in self.store.models:
            row = ModelRow(self, model)
            self._rows[model.id] = row
            group.add(row)
        page.add(group)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(page)
        self.append(scroller)

        self.settings.connect("changed::active-model", lambda *_: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        active = self.settings.get_string("active-model")
        for model_id, row in self._rows.items():
            row.refresh(active == model_id, model_id in self._downloads)

    # -- actions ---------------------------------------------------------

    def select(self, model) -> None:
        self.settings.set_string("active-model", model.id)
        self.refresh()

    def download(self, model) -> None:
        if model.id in self._downloads:
            return
        row = self._rows[model.id]
        row.begin_download()

        def progress(received: int, total: int) -> None:
            row.set_progress(received, total)

        def done(ok: bool, error: str) -> None:
            self._downloads.pop(model.id, None)
            row.end_download()
            if ok:
                if not self.settings.get_string("active-model"):
                    self.settings.set_string("active-model", model.id)
                self.app.notifier.notify(
                    "scribe-model", "Model ready", f"{model.name} is ready to use."
                )
            else:
                self._toast(f"Could not download {model.name}: {error}")
            self.refresh()

        dl = Download(self.store, model, on_progress=progress, on_done=done)
        self._downloads[model.id] = dl
        dl.start()
        self.refresh()

    def cancel_download(self, model) -> None:
        dl = self._downloads.get(model.id)
        if dl:
            dl.cancel()

    def delete(self, model) -> None:
        self.store.delete(model)
        if self.settings.get_string("active-model") == model.id:
            remaining = self.store.downloaded()
            self.settings.set_string(
                "active-model", remaining[0].id if remaining else ""
            )
        self.refresh()

    def _toast(self, message: str) -> None:
        window = self.get_root()
        if hasattr(window, "toast"):
            window.toast(message)


class ModelRow(Adw.ActionRow):
    def __init__(self, page: ModelsPage, model) -> None:
        languages = "English only" if not model.multilingual else \
            f"{model.languages} languages"
        super().__init__(
            title=model.name,
            subtitle=f"{model.size_label} · {languages} · {TIER_LABEL.get(model.tier, '')}",
        )
        self.page = page
        self.model = model

        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER, width_request=120)
        self.progress.set_visible(False)

        self.use_button = Gtk.Button(label="Use", valign=Gtk.Align.CENTER)
        self.use_button.connect("clicked", lambda *_: page.select(model))

        self.get_button = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
        self.get_button.add_css_class("suggested-action")
        self.get_button.connect("clicked", lambda *_: page.download(model))

        self.cancel_button = Gtk.Button(
            icon_name="process-stop-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Cancel download",
        )
        self.cancel_button.add_css_class("flat")
        self.cancel_button.connect("clicked", lambda *_: page.cancel_download(model))

        self.delete_button = Gtk.Button(
            icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Remove this model",
        )
        self.delete_button.add_css_class("flat")
        self.delete_button.connect("clicked", lambda *_: page.delete(model))

        self.active_icon = Gtk.Image(
            icon_name="object-select-symbolic", valign=Gtk.Align.CENTER
        )

        for w in (self.progress, self.active_icon, self.use_button,
                  self.get_button, self.cancel_button, self.delete_button):
            self.add_suffix(w)

    def refresh(self, is_active: bool, downloading: bool) -> None:
        present = self.page.store.is_downloaded(self.model)
        self.active_icon.set_visible(present and is_active)
        self.use_button.set_visible(present and not is_active)
        self.get_button.set_visible(not present and not downloading)
        self.delete_button.set_visible(present and not downloading)
        self.cancel_button.set_visible(downloading)
        self.progress.set_visible(downloading)

    def begin_download(self) -> None:
        self.progress.set_fraction(0.0)
        self.progress.set_visible(True)

    def set_progress(self, received: int, total: int) -> None:
        if total:
            self.progress.set_fraction(min(1.0, received / total))
            self.progress.set_text(
                f"{GLib.format_size(received)} of {GLib.format_size(total)}"
            )

    def end_download(self) -> None:
        self.progress.set_visible(False)
