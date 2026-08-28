"""What was dictated recently, and nothing older."""

from __future__ import annotations

import time

from gi.repository import Adw, Gtk

from relative_time import describe

# Below this many entries a search box is clutter rather than help.
SEARCH_THRESHOLD = 8


class HistoryPage(Gtk.Box):
    def __init__(self, application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = application
        self.settings = application.settings
        self.history = application.history

        self.search = Gtk.SearchEntry(
            placeholder_text="Search transcripts", margin_top=12,
            margin_bottom=0, margin_start=18, margin_end=18,
        )
        self.search.connect("search-changed", lambda *_: self._render())
        self.search.set_visible(False)
        self.append(self.search)

        self.empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="Nothing dictated yet",
            description="What you dictate will be listed here.",
            vexpand=True,
        )

        self.page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup()
        self.page.add(self.group)
        self._rows: list[Gtk.Widget] = []

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.page)

        self.stack = Gtk.Stack(vexpand=True)
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(scroller, "list")
        self.append(self.stack)

        self.settings.connect("changed::history-limit", lambda *_: self.reload())
        self.reload()

    # -- rendering -------------------------------------------------------

    def reload(self) -> None:
        self._render()

    def _limit_description(self) -> str:
        if not self.settings.get_boolean("history-enabled"):
            return "History is turned off, so nothing is being stored."
        limit = self.settings.get_int("history-limit")
        if limit <= 0:
            return "Nothing is kept."
        entries = "dictation" if limit == 1 else "dictations"
        return (
            f"Only the last {limit} {entries} are kept. Anything older is erased "
            f"from this computer, not just hidden."
        )

    def _render(self) -> None:
        query = self.search.get_text()
        entries = self.history.search(query) if query else self.history.recent()
        total = self.history.count()

        self.search.set_visible(total > SEARCH_THRESHOLD)

        for row in self._rows:
            self.group.remove(row)
        self._rows.clear()

        self.group.set_title("Recent" if not query else "Matching")
        self.group.set_description(self._limit_description())

        if not entries:
            self.empty.set_title(
                "No matching transcripts" if query else "Nothing dictated yet"
            )
            self.empty.set_description(
                "Try a different search."
                if query else self._limit_description()
            )
            self.stack.set_visible_child_name("empty")
            return

        now = time.time()
        for entry in entries:
            row = HistoryRow(self, entry, now)
            self.group.add(row)
            self._rows.append(row)

        clear = Gtk.Button(label="Clear History", halign=Gtk.Align.CENTER,
                           margin_top=6)
        clear.add_css_class("destructive-action")
        clear.connect("clicked", lambda *_: self._confirm_clear())
        self.group.add(clear)
        self._rows.append(clear)

        self.stack.set_visible_child_name("list")

    # -- actions ---------------------------------------------------------

    def delete(self, entry) -> None:
        self.history.delete(entry.id)
        self._render()

    def copy(self, entry) -> None:
        self.get_clipboard().set(entry.text)
        window = self.get_root()
        if hasattr(window, "toast"):
            window.toast("Copied to clipboard")

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
        dialog.connect("response", self._on_clear_response)
        dialog.present(self.get_root())

    def _on_clear_response(self, _dialog, response: str) -> None:
        if response != "clear":
            return
        self.history.clear()
        self._render()
        window = self.get_root()
        if hasattr(window, "toast"):
            window.toast("History cleared")


class HistoryRow(Adw.ActionRow):
    def __init__(self, page: HistoryPage, entry, now: float) -> None:
        detail = describe(now - entry.created_at)
        if entry.duration_ms:
            detail += f" · {entry.duration_ms / 1000:.1f}s"
        if entry.language and entry.language != "auto":
            detail += f" · {entry.language}"

        super().__init__(title=entry.text, subtitle=detail)
        self.set_title_lines(3)
        self.set_subtitle_lines(1)
        self.set_use_markup(False)

        copy = Gtk.Button(
            icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
            css_classes=["flat"], tooltip_text="Copy",
        )
        copy.connect("clicked", lambda *_: page.copy(entry))
        self.add_suffix(copy)

        remove = Gtk.Button(
            icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
            css_classes=["flat"], tooltip_text="Delete",
        )
        remove.connect("clicked", lambda *_: page.delete(entry))
        self.add_suffix(remove)
