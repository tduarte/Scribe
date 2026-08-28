"""Everything dictated so far, searchable."""

from __future__ import annotations

from gi.repository import Adw, Gtk


class HistoryPage(Gtk.Box):
    def __init__(self, application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = application
        self.history = application.history

        self.search = Gtk.SearchEntry(
            placeholder_text="Search transcripts", margin_top=12,
            margin_bottom=6, margin_start=12, margin_end=12,
        )
        self.search.connect("search-changed", lambda *_: self.reload())
        self.append(self.search)

        self.empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="Nothing dictated yet",
            description="Transcripts you dictate will be listed here.",
            vexpand=True,
        )

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.set_margin_start(12)
        self.list.set_margin_end(12)
        self.list.set_margin_bottom(12)
        self.list.set_valign(Gtk.Align.START)

        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_child(self.list)

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.scroller, "list")
        self.append(self.stack)

        self.reload()

    def reload(self) -> None:
        query = self.search.get_text()
        entries = self.history.search(query) if query else self.history.recent()

        child = self.list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.list.remove(child)
            child = nxt

        for entry in entries:
            self.list.append(HistoryRow(self, entry))

        if entries:
            self.stack.set_visible_child_name("list")
        else:
            self.empty.set_title(
                "No matching transcripts" if query else "Nothing dictated yet"
            )
            self.stack.set_visible_child_name("empty")

    def delete(self, entry) -> None:
        self.history.delete(entry.id)
        self.reload()

    def copy(self, entry) -> None:
        self.get_clipboard().set(entry.text)
        window = self.get_root()
        if hasattr(window, "toast"):
            window.toast("Copied to clipboard")


class HistoryRow(Adw.ActionRow):
    def __init__(self, page: HistoryPage, entry) -> None:
        when = entry.when.format("%e %b %Y, %H:%M").strip()
        seconds = entry.duration_ms / 1000
        detail = f"{when} · {seconds:.1f}s"
        if entry.language:
            detail += f" · {entry.language}"
        super().__init__(title=entry.text, subtitle=detail)
        self.set_title_lines(3)
        self.set_use_markup(False)

        copy = Gtk.Button(
            icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Copy",
        )
        copy.add_css_class("flat")
        copy.connect("clicked", lambda *_: page.copy(entry))
        self.add_suffix(copy)

        remove = Gtk.Button(
            icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text="Delete",
        )
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_: page.delete(entry))
        self.add_suffix(remove)
