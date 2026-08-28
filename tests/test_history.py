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
