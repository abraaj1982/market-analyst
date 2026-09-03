"""Timeframe resampling and closed-candle enforcement.

Two responsibilities, both of them safety-critical:

1. **Building higher timeframes correctly.** No free provider serves 4H candles
   for FX or metals, so 4H is aggregated from 1H. Aggregation must be anchored
   to a fixed clock (00:00/04:00/08:00... UTC), otherwise the boundaries drift
   with whatever the first bar of the download happened to be, and every
   structure level shifts with it.

2. **Dropping the forming candle.** The most recent bar from any provider is
   almost always still open. Analysing it means the analysis silently changes
   as the bar develops, and any historical study of it is fiction.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from analyst.core.clock import ensure_utc_index, now_utc
from analyst.core.enums import Timeframe

#: How to collapse each OHLCV column when aggregating.
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample(df: pd.DataFrame, target: Timeframe) -> pd.DataFrame:
    """Aggregate `df` into `target`, anchored to the UTC epoch.

    `label="left"` + `closed="left"` means a bar stamped 08:00 covers
    [08:00, 12:00) — the same convention every charting platform uses.
    """
    df = ensure_utc_index(df)
    # `origin` only applies to tick-like frequencies; passing it for D/W raises a
    # warning and is a no-op, since those already start at a natural boundary.
    kwargs = {"origin": "epoch"} if target.minutes < 1440 else {}
    out = (
        df.resample(target.pandas_rule, label="left", closed="left", **kwargs)
        .agg(_AGG)
        .dropna(subset=["open", "high", "low", "close"])
    )
    return out


def drop_forming_candle(df: pd.DataFrame, tf: Timeframe, as_of: datetime | None = None) -> pd.DataFrame:
    """Remove the trailing bar if its period has not elapsed yet.

    A bar stamped T covers [T, T + tf). It is closed only once `as_of >= T + tf`.
    """
    if df.empty:
        return df
    as_of = as_of or now_utc()
    period = timedelta(minutes=tf.minutes)
    last_open = df.index[-1].to_pydatetime()
    if last_open + period > as_of:
        return df.iloc[:-1]
    return df


def build_timeframes(
    base: pd.DataFrame,
    base_tf: Timeframe,
    targets: list[Timeframe],
    as_of: datetime | None = None,
) -> dict[Timeframe, tuple[pd.DataFrame, bool]]:
    """Produce every requested timeframe from a single base series.

    Returns `{tf: (frame, derived)}` where `derived` is True when the frame was
    resampled rather than fetched natively — surfaced in the report so the
    reader knows which levels came from real provider candles.
    """
    out: dict[Timeframe, tuple[pd.DataFrame, bool]] = {}
    for tf in targets:
        if tf.minutes < base_tf.minutes:
            continue  # cannot invent detail we never had
        frame = base if tf is base_tf else resample(base, tf)
        out[tf] = (drop_forming_candle(frame, tf, as_of), tf is not base_tf)
    return out
