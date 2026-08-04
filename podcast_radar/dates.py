"""Date and duration parsing shared by feeds, collectors, and storage.

Every medium reports timestamps and runtimes differently: RSS sends RFC 2822
dates and ``HH:MM:SS`` strings, the YouTube Data API sends ISO 8601 for both,
yt-dlp sends a number of seconds, and SQLite hands back whatever was stored.
Normalising in one place keeps the cross-medium duplicate comparisons in
storage and collectors from drifting apart.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any


def parse_datetime(value: Any) -> dt.datetime | None:
    """Parse an ISO 8601 timestamp into an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def parse_cutoff(value: str | None) -> dt.datetime | None:
    """Parse a ``--since`` value, which may be a bare date or a full timestamp."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = dt.datetime.fromisoformat(value + "T00:00:00+00:00")
    return _as_utc(parsed)


def is_since(published_at: Any, since: dt.datetime) -> bool:
    """Whether an item is at or after the cutoff.

    Undated and unparseable items are kept: dropping them would silently hide
    sources whose feeds omit publication dates.
    """
    if not published_at:
        return True
    parsed = parse_datetime(published_at)
    if parsed is None:
        return True
    return parsed >= since


def datetimes_close(first: Any, second: Any, *, days: int) -> bool:
    left = parse_datetime(first)
    right = parse_datetime(second)
    if left is None or right is None:
        return False
    return abs((left - right).total_seconds()) <= days * 86400


def duration_seconds(value: Any) -> int | None:
    """Read a runtime from seconds, ``MM:SS``/``HH:MM:SS``, or ISO 8601."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if all(part.isdigit() for part in parts):
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        return total
    return _iso_8601_seconds(text)


def durations_close(first: Any, second: Any) -> bool:
    """Whether two runtimes plausibly describe the same recording.

    Cross-posted audio and video rarely match to the second, so the tolerance
    is the larger of two minutes and 12% of the shorter runtime.
    """
    left = duration_seconds(first)
    right = duration_seconds(second)
    if left is None or right is None:
        return False
    return abs(left - right) <= max(120, min(left, right) * 0.12)


_ISO_DURATION = re.compile(
    r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)


def _iso_8601_seconds(value: str) -> int | None:
    match = _ISO_DURATION.fullmatch(value)
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = match.groups()
    return int(
        int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)
    )


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)
