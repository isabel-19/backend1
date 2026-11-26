from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Current instant as an aware UTC datetime.

    ``datetime.utcnow()`` is naive. SQLModel 0.0.45+ rejects naive values on
    write (``UTCDateTime``), so every timestamp this app stores or compares
    goes through this helper.
    """
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalize a datetime to aware UTC.

    Naive values are treated as UTC. That is how this database was written
    (``datetime.utcnow()``) and how clients that omit an offset were stored.
    Aware values are converted to UTC so comparisons never mix kinds.
    """
    if value is None:
        return None
    if value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
