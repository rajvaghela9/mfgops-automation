"""Timestamp helper shared by every store, so all dates are UTC ISO strings."""

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Give the current UTC time as an ISO-8601 string (seconds precision).

    Returns:
        A string like "2026-10-05T14:03:22+00:00".
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
