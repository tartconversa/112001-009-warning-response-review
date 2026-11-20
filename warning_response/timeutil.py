"""Timestamp parsing and formatting helpers.

All event data uses local, timezone-naive ISO 8601 timestamps (for example
``2026-07-12T09:00``).  Mixing timezone-aware and naive values would make
ordering ambiguous, so aware values are rejected with an explicit error.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .errors import InputDataError

ONE_MICROSECOND = timedelta(microseconds=1)


def parse_time(value: str, *, field: str, allow_empty: bool = False) -> datetime | None:
    """Parse a local ISO 8601 timestamp.

    ``allow_empty`` turns an empty value into ``None``.  Timezones are
    rejected because the datasets describe one local operating area.
    """

    text = (value or "").strip()
    if not text:
        if allow_empty:
            return None
        raise InputDataError(f"{field} 不能为空", code="invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InputDataError(
            f"{field} 不是有效的 ISO 8601 时间: {text!r}", code="invalid_timestamp"
        ) from exc
    if parsed.tzinfo is not None:
        raise InputDataError(
            f"{field} 不能带时区（请统一使用现场本地时间）: {text!r}",
            code="invalid_timestamp",
        )
    return parsed


def format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.microsecond:
        return value.isoformat(timespec="microseconds")
    return value.isoformat(timespec="seconds")


def just_before(value: datetime) -> datetime:
    return value - ONE_MICROSECOND


def minutes_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0
