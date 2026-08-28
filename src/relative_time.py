"""Human phrasing for when something happened."""

from __future__ import annotations

MINUTE = 60
HOUR = 3600
DAY = 86400


def describe(seconds_ago: float) -> str:
    """Phrase an age the way a person would say it.

    Deliberately coarse: for a list of recent dictations, "5 minutes ago" is
    more useful than a timestamp, and precision beyond that is noise.
    """
    if seconds_ago < 0:
        return "just now"
    if seconds_ago < 10:
        return "just now"
    if seconds_ago < MINUTE:
        return f"{int(seconds_ago)} seconds ago"
    if seconds_ago < 2 * MINUTE:
        return "a minute ago"
    if seconds_ago < HOUR:
        return f"{int(seconds_ago // MINUTE)} minutes ago"
    if seconds_ago < 2 * HOUR:
        return "an hour ago"
    if seconds_ago < DAY:
        return f"{int(seconds_ago // HOUR)} hours ago"
    if seconds_ago < 2 * DAY:
        return "yesterday"
    if seconds_ago < 7 * DAY:
        return f"{int(seconds_ago // DAY)} days ago"
    if seconds_ago < 14 * DAY:
        return "last week"
    if seconds_ago < 60 * DAY:
        return f"{int(seconds_ago // (7 * DAY))} weeks ago"
    return "a long time ago"
