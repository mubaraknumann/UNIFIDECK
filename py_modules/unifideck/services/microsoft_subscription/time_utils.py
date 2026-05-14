"""Time helpers — end-of-month UTC expiry, ISO formatting."""

from __future__ import annotations
from datetime import UTC, datetime
def _end_of_month_utc(now: datetime | None = None) -> float:
    """Compute the Unix timestamp of the UTC end-of-month.

    Used as the natural expiry for Microsoft Game Pass
    subscription caches.

    Args:
        now: Reference datetime (default: ``datetime.now(UTC)``).

    Returns:
        Unix timestamp (float) of midnight UTC on the 1st
        of the following month.
    """
    now = now if now is not None else datetime.now(UTC)
    if now.month == 12:
        nxt = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        nxt = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    return nxt.timestamp()
def _fmt_ts(ts: float) -> str:
    """Format a Unix timestamp as an ISO-8601 UTC string.

    Convenience helper used in debug logs.

    Args:
        ts: Unix timestamp.

    Returns:
        ISO-8601 string with explicit ``+00:00`` offset.
    """
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()