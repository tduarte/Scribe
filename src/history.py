"""A searchable record of everything dictated.

sqlite3 is in the Python standard library shipped by the GNOME runtime, so this
costs no dependency. Writes happen on the main loop; the rows are tiny and the
inserts are one-per-utterance, so this never blocks perceptibly.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass

from gi.repository import GLib

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Entry:
    id: int
    created_at: float
    text: str
    duration_ms: int
    model: str
    language: str

    @property
    def when(self) -> GLib.DateTime:
        return GLib.DateTime.new_from_unix_local(int(self.created_at))


class History:
    def __init__(self, path: str | None = None) -> None:
        if path is None:
            path = os.path.join(GLib.get_user_data_dir(), "history.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        cur = self.db.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS transcripts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at  REAL    NOT NULL,
                text        TEXT    NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                model       TEXT    NOT NULL DEFAULT '',
                language    TEXT    NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_transcripts_created "
            "ON transcripts(created_at DESC)"
        )
        cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.commit()

    def add(
        self, text: str, *, duration_ms: int = 0, model: str = "", language: str = ""
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO transcripts (created_at, text, duration_ms, model, language) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), text, duration_ms, model, language),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def recent(self, limit: int = 100, offset: int = 0) -> list[Entry]:
        rows = self.db.execute(
            "SELECT * FROM transcripts ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._entry(r) for r in rows]

    def search(self, query: str, limit: int = 100) -> list[Entry]:
        if not query.strip():
            return self.recent(limit)
        rows = self.db.execute(
            "SELECT * FROM transcripts WHERE text LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [self._entry(r) for r in rows]

    def latest(self) -> Entry | None:
        row = self.db.execute(
            "SELECT * FROM transcripts ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return self._entry(row) if row else None

    def delete(self, entry_id: int) -> None:
        self.db.execute("DELETE FROM transcripts WHERE id = ?", (entry_id,))
        self.db.commit()

    def clear(self) -> None:
        self.db.execute("DELETE FROM transcripts")
        self.db.commit()

    def prune(self, retention_days: int) -> int:
        """Drop entries older than the retention window. 0 keeps everything."""
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        cur = self.db.execute("DELETE FROM transcripts WHERE created_at < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0])

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _entry(row: sqlite3.Row) -> Entry:
        return Entry(
            id=row["id"],
            created_at=row["created_at"],
            text=row["text"],
            duration_ms=row["duration_ms"],
            model=row["model"],
            language=row["language"],
        )
