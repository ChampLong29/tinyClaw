"""Timezone helpers for user-facing time display.

Storage can remain UTC, while user-facing output is normalized to Beijing time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


BEIJING_TZ = timezone(timedelta(hours=8), name="CST")


def now_beijing() -> datetime:
    """Return current aware datetime in Beijing timezone."""
    return datetime.now(BEIJING_TZ)


def format_beijing(dt: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format any datetime as Beijing time string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ).strftime(fmt)


def format_iso_to_beijing(iso_str: str, fmt: str = "%Y-%m-%d %H:%M", empty: str = "无") -> str:
    """Parse ISO time string and format it in Beijing timezone."""
    if not iso_str:
        return empty
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return format_beijing(dt, fmt=fmt)
    except ValueError:
        return iso_str
