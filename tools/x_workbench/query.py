"""query components; no task starts on import."""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def build_search_query(keyword: str, start_date: date | None, end_date: date | None, timezone_offset: float = 8) -> str:
    parts = [keyword.strip()]
    local_tz = timezone(timedelta(hours=timezone_offset))
    if start_date:
        lower = datetime.combine(start_date, datetime.min.time(), tzinfo=local_tz)
        parts.append(f"since_time:{int(lower.timestamp())}")
    if end_date:
        # Half-open instant range implements an inclusive local calendar end date.
        upper = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=local_tz)
        parts.append(f"until_time:{int(upper.timestamp())}")
    return " ".join(parts)


def post_in_date_range(posted_at: str, start_date: date | None, end_date: date | None, timezone_offset: float = 8) -> bool:
    if not posted_at:
        return True
    try:
        local_tz = timezone(timedelta(hours=timezone_offset))
        value = datetime.fromisoformat(posted_at.replace("Z", "+00:00")).astimezone(local_tz).date()
    except ValueError:
        return True
    return (not start_date or value >= start_date) and (not end_date or value <= end_date)


def make_date_slices(start_date: date | None, end_date: date | None, slice_days: int) -> list[tuple[date | None, date | None]]:
    if not start_date or not end_date or slice_days <= 0:
        return [(start_date, end_date)]
    output: list[tuple[date, date]] = []
    cursor = start_date
    while cursor <= end_date:
        slice_end = min(end_date, cursor + timedelta(days=slice_days - 1))
        output.append((cursor, slice_end))
        cursor = slice_end + timedelta(days=1)
    return output
