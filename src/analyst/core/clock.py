"""Time handling. One rule: compute in UTC, display in the configured zone.

Every timestamp that enters the system is normalised here. Mixing naive and
aware datetimes is the single most common source of silent off-by-hours bugs in
market systems, so there is exactly one entry point for it.
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pandas as pd

UTC = UTC


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime | pd.Timestamp) -> datetime:
    """Normalise any datetime to tz-aware UTC. Naive input is *assumed* UTC."""
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_display(value: datetime, tz_name: str) -> datetime:
    return to_utc(value).astimezone(ZoneInfo(tz_name))


def format_display(value: datetime, tz_name: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return to_display(value, tz_name).strftime(fmt)


def ensure_utc_index(df: pd.DataFrame) -> pd.DataFrame:
    """Return `df` with a tz-aware UTC DatetimeIndex, sorted and de-duplicated."""
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize(UTC) if idx.tz is None else idx.tz_convert(UTC)
    out = df.copy()
    out.index = idx
    out = out.sort_index()
    return out[~out.index.duplicated(keep="last")]
