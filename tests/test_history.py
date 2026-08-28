import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import time
import pytest
from history import History


@pytest.fixture
def hist():
    with tempfile.TemporaryDirectory() as d:
        h = History(os.path.join(d, "h.db"))
        yield h
        h.close()


def test_add_and_read_back(hist):
    hist.add("hello world", duration_ms=1200, model="turbo", language="en")
    entries = hist.recent()
    assert len(entries) == 1
    assert entries[0].text == "hello world"
    assert entries[0].duration_ms == 1200


def test_recent_is_newest_first(hist):
    for t in ["first", "second", "third"]:
        hist.add(t)
        time.sleep(0.005)
    assert [e.text for e in hist.recent()] == ["third", "second", "first"]


def test_latest(hist):
    hist.add("old"); time.sleep(0.005); hist.add("new")
    assert hist.latest().text == "new"


def test_latest_on_empty_db(hist):
    assert hist.latest() is None


def test_search_matches_substring(hist):
    hist.add("deploy to production")
    hist.add("unrelated note")
    assert [e.text for e in hist.search("deploy")] == ["deploy to production"]


def test_blank_search_returns_everything(hist):
    hist.add("a"); hist.add("b")
    assert len(hist.search("   ")) == 2


def test_delete(hist):
    eid = hist.add("goodbye")
    hist.delete(eid)
    assert hist.count() == 0


def test_prune_removes_only_old_entries(hist):
    hist.add("recent")
    hist.db.execute(
        "INSERT INTO transcripts (created_at, text) VALUES (?, ?)",
        (time.time() - 40 * 86400, "ancient"),
    )
    hist.db.commit()
    assert hist.count() == 2
    removed = hist.prune(retention_days=30)
    assert removed == 1
    assert [e.text for e in hist.recent()] == ["recent"]


def test_prune_zero_keeps_everything(hist):
    hist.db.execute(
        "INSERT INTO transcripts (created_at, text) VALUES (?, ?)",
        (time.time() - 999 * 86400, "ancient"),
    )
    hist.db.commit()
    assert hist.prune(0) == 0
    assert hist.count() == 1


def test_reopen_persists(hist):
    hist.add("durable")
    path = hist.path
    hist.close()
    again = History(path)
    assert again.latest().text == "durable"
    again.close()


class TestDestructiveLimit:
    """The limit is a privacy guarantee, so it must reach the file on disk."""

    def test_keeps_only_the_newest_n(self, hist):
        for i in range(12):
            hist.add(f"entry {i}")
            time.sleep(0.002)
        removed = hist.enforce_limit(5)
        assert removed == 7
        assert hist.count() == 5
        assert [e.text for e in hist.recent()] == [
            "entry 11", "entry 10", "entry 9", "entry 8", "entry 7"
        ]

    def test_under_the_limit_is_a_noop(self, hist):
        hist.add("only one")
        assert hist.enforce_limit(5) == 0
        assert hist.count() == 1

    def test_zero_limit_erases_everything(self, hist):
        for i in range(4):
            hist.add(f"secret {i}")
        assert hist.enforce_limit(0) == 4
        assert hist.count() == 0

    def test_purged_text_is_not_left_in_the_file(self, hist):
        # secure_delete + WAL checkpoint + VACUUM should leave no readable
        # trace of a dropped transcript in the database file.
        hist.add("KEEPME the newest entry")
        for i in range(6):
            hist.add(f"PURGEME sensitive utterance {i}")
            time.sleep(0.002)
        hist.add("KEEPME the newest entry")
        hist.enforce_limit(1)
        assert hist.count() == 1

        with open(hist.path, "rb") as fh:
            raw = fh.read()
        assert b"PURGEME" not in raw, "deleted transcript text survived in the db file"

    def test_wal_sidecar_does_not_retain_purged_text(self, hist):
        import os
        hist.add("PURGEME confidential")
        hist.add("kept")
        hist.enforce_limit(1)
        wal = hist.path + "-wal"
        if os.path.exists(wal):
            with open(wal, "rb") as fh:
                assert b"PURGEME" not in fh.read(), "purged text survived in the WAL"

    def test_lowering_the_limit_later_erases_the_excess(self, hist):
        for i in range(10):
            hist.add(f"entry {i}")
            time.sleep(0.002)
        hist.enforce_limit(8)
        assert hist.count() == 8
        hist.enforce_limit(3)
        assert hist.count() == 3

    def test_survives_reopen(self, hist):
        for i in range(9):
            hist.add(f"entry {i}")
            time.sleep(0.002)
        hist.enforce_limit(5)
        path = hist.path
        hist.close()
        again = History(path)
        assert again.count() == 5
        again.close()
