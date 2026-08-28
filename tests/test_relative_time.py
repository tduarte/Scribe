import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from relative_time import describe


@pytest.mark.parametrize("seconds,expected", [
    (0, "just now"),
    (5, "just now"),
    (30, "30 seconds ago"),
    (75, "a minute ago"),
    (300, "5 minutes ago"),
    (3600, "an hour ago"),
    (7500, "2 hours ago"),
    (90000, "yesterday"),
    (3 * 86400, "3 days ago"),
    (8 * 86400, "last week"),
    (21 * 86400, "3 weeks ago"),
    (400 * 86400, "a long time ago"),
])
def test_phrasing(seconds, expected):
    assert describe(seconds) == expected


def test_clock_skew_does_not_produce_negative_ages():
    assert describe(-30) == "just now"


def test_never_returns_an_empty_string():
    for s in (0, 1, 59, 60, 3599, 3600, 86399, 86400, 10**7):
        assert describe(s).strip()
