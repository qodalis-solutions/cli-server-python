"""Query time ranges of the dashboard's chart endpoints (``from``/``to``,
``buckets``), parsed the same way on every server."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

DEFAULT_BUCKETS = 24
MAX_BUCKETS = 200
DEFAULT_RANGE = timedelta(hours=24)


class RangeError(ValueError):
    """A bad query value (answered with 400 ``{"error": ...}``)."""


def parse_time(value: str | None, name: str) -> datetime | None:
    """An ISO 8601 query time as aware UTC (no offset = UTC); ``None`` when
    absent; :class:`RangeError` when not a time."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise RangeError(f"'{name}' must be an ISO 8601 time, got '{value}'") from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_buckets(value: str | None) -> int:
    """``buckets`` clamped to 1..200 (default 24)."""
    if value is None or not value.strip():
        return DEFAULT_BUCKETS
    try:
        count = int(value.strip())
    except ValueError:
        raise RangeError(f"'buckets' must be an integer, got '{value}'") from None
    return min(max(count, 1), MAX_BUCKETS)


def resolve_range(
    start: str | None,
    end: str | None,
    *,
    start_name: str = "from",
    end_name: str = "to",
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """``[start, end)``: the end defaults to now, the start to end - 24h."""
    until = parse_time(end, end_name) or (now or datetime.now(timezone.utc))
    since = parse_time(start, start_name) or until - DEFAULT_RANGE
    if since >= until:
        raise RangeError(f"'{start_name}' must be before '{end_name}'")
    return since, until
