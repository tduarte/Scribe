"""A short, deliberately forgetful record of what was dictated.

Only the most recent few transcripts are kept, and anything past that limit is
genuinely destroyed rather than merely hidden. That takes more than a DELETE:

* `secure_delete` makes SQLite overwrite removed content with zeros instead of
  leaving it legible in free pages.
* WAL keeps old page images in a side file, so the log is checkpointed and
  truncated after a purge.
* VACUUM rewrites the database so freed pages cannot be recovered.

sqlite3 ships with the GNOME runtime's Python, so this costs no dependency.
Writes happen on the main loop; the rows are tiny and there is one insert per
utterance, so it never blocks perceptibly.
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

# LIKE treats % and _ as wildcards, so a search for "100%" would otherwise
# match every stored transcript. Escaped with a backslash, declared per query.
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


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
        # Must be set before any deletion for it to have any effect.
        self.db.execute("PRAGMA secure_delete = ON")
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
            "SELECT * FROM transcripts WHERE text LIKE ? ESCAPE '\\' "
            "ORDER BY created_at DESC LIMIT ?",
            (f"%{query.translate(_LIKE_ESCAPE)}%", limit),
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
        self._scrub()

    def enforce_limit(self, limit: int) -> int:
        """Keep only the newest `limit` entries, destroying the rest.

        `limit` of 0 or less means "keep nothing", which is what disabling
        history does -- it purges what is already stored rather than just
        stopping new writes.
        """
        if limit <= 0:
            removed = self.count()
            if removed:
                self.db.execute("DELETE FROM transcripts")
                self.db.commit()
                self._scrub()
            return removed

        cur = self.db.execute(
            "DELETE FROM transcripts WHERE id NOT IN ("
            "  SELECT id FROM transcripts ORDER BY created_at DESC, id DESC LIMIT ?"
            ")",
            (limit,),
        )
        removed = cur.rowcount
        self.db.commit()
        if removed > 0:
            self._scrub()
        return removed

    def _scrub(self) -> None:
        """Make deleted rows unrecoverable from the file on disk."""
        try:
            # Fold the write-ahead log back in and truncate it, so old page
            # images do not survive in the -wal sidecar.
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # VACUUM rewrites the file, releasing the freed pages entirely.
            # It cannot run inside a transaction, hence the commit above.
            self.db.execute("VACUUM")
        except sqlite3.Error as exc:
            log.warning("could not scrub deleted history: %s", exc)

    def prune(self, retention_days: int) -> int:
        """Drop entries older than the retention window. 0 keeps everything."""
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        cur = self.db.execute("DELETE FROM transcripts WHERE created_at < ?", (cutoff,))
        removed = cur.rowcount
        self.db.commit()
        if removed > 0:
            self._scrub()
        return removed

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
