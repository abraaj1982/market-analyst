"""Trend and moving-average indicators.

All functions are pure, vectorised, and return a Series aligned to the input
index with NaN during the warm-up period. Nothing here looks ahead: every value
at index *i* is computable from bars <= i only.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential MA with `adjust=False` — the recursive form platforms use."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA): alpha = 1/period. Used by RSI, ATR, ADX."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def slope_normalised(series: pd.Series, period: int, atr: pd.Series) -> pd.Series:
    """Slope of `series` over `period` bars, expressed in ATR units per bar.

    Normalising by ATR makes the number comparable across instruments priced in
    wildly different units (2400 for gold vs 1.08 for EURUSD).
    """
    raw = (series - series.shift(period)) / period
    return raw / atr.replace(0.0, np.nan)


def ichimoku(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    tenkan: int = 9,
    kijun: int = 26,
    senkou_b: int = 52,
) -> pd.DataFrame:
    """Ichimoku Kinko Hyo.

    The cloud is shifted FORWARD by `kijun` bars, which is exactly what makes it
    usable without lookahead: at bar *i* the cloud plotted at *i* was computed
    from data at *i - kijun*. `chikou` is deliberately NOT used for signals, as
    reading it at the current bar requires future prices.
    """
    conv = (high.rolling(tenkan).max() + low.rolling(tenkan).min()) / 2
    base = (high.rolling(kijun).max() + low.rolling(kijun).min()) / 2
    span_a = ((conv + base) / 2).shift(kijun)
    span_b = ((high.rolling(senkou_b).max() + low.rolling(senkou_b).min()) / 2).shift(kijun)
    return pd.DataFrame(
        {
            "tenkan": conv,
            "kijun": base,
            "span_a": span_a,
            "span_b": span_b,
            "cloud_top": pd.concat([span_a, span_b], axis=1).max(axis=1),
            "cloud_bottom": pd.concat([span_a, span_b], axis=1).min(axis=1),
        }
    )


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.DataFrame:
    """Wilder's ADX with +DI / -DI.

    ADX measures trend *strength* regardless of direction; +DI/-DI carry the
    sign. The regime detector uses ADX, the trend engine uses the DI spread.
    """
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)

    tr = true_range(high, low, close)
    atr_ = wilder_smooth(tr, period)

    plus_di = 100.0 * wilder_smooth(plus_dm, period) / atr_.replace(0.0, np.nan)
    minus_di = 100.0 * wilder_smooth(minus_dm, period) / atr_.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return pd.DataFrame({"adx": wilder_smooth(dx, period), "plus_di": plus_di, "minus_di": minus_di})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return wilder_smooth(true_range(high, low, close), period)


def supertrend(
    high: pd.Series, low: pd.Series, close: pd.Series,
    period: int = 10, multiplier: float = 3.0,
) -> pd.DataFrame:
    """SuperTrend: an ATR-banded trailing stop that flips side on a close
    beyond the opposite band.

    Each bar's line depends on the previous bar's line and side (a band only
    tightens toward price, and a flip only happens on a genuine breakout), so
    this is inherently sequential rather than something a rolling-window
    vectorisation can express -- one pass over the series, same as any other
    trailing-stop definition.

    Returns a frame with `line` (the stop level) and `direction` (1 while it
    trails below price / bullish, -1 while it trails above / bearish).
    """
    atr_series = atr(high, low, close, period)
    hl2 = (high + low) / 2
    basic_upper = (hl2 + multiplier * atr_series).to_numpy()
    basic_lower = (hl2 - multiplier * atr_series).to_numpy()
    close_v = close.to_numpy()

    n = len(close)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    line = np.full(n, np.nan)
    direction = np.zeros(n, dtype=int)

    for i in range(n):
        if np.isnan(basic_upper[i]) or np.isnan(basic_lower[i]):
            continue
        if np.isnan(final_upper[i - 1]) if i > 0 else True:
            final_upper[i], final_lower[i] = basic_upper[i], basic_lower[i]
            direction[i] = 1 if close_v[i] >= final_lower[i] else -1
        else:
            final_upper[i] = (
                basic_upper[i]
                if basic_upper[i] < final_upper[i - 1] or close_v[i - 1] > final_upper[i - 1]
                else final_upper[i - 1]
            )
            final_lower[i] = (
                basic_lower[i]
                if basic_lower[i] > final_lower[i - 1] or close_v[i - 1] < final_lower[i - 1]
                else final_lower[i - 1]
            )
            if direction[i - 1] == 1:
                direction[i] = -1 if close_v[i] < final_lower[i] else 1
            else:
                direction[i] = 1 if close_v[i] > final_upper[i] else -1

        line[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return pd.DataFrame({"line": line, "direction": direction}, index=close.index)


def atr_percent(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR expressed as a percentage of price.

    Raw ATR is denominated in price units, so on any instrument that has trended
    a long way it drifts upward simply because the price is higher. Taking a
    percentile of raw ATR therefore reports "record volatility" at the top of
    every multi-year uptrend and "dead calm" at the bottom of every downtrend -
    a structural bias, not a market observation.

    Normalising by price removes it, and is what makes a volatility percentile
    comparable across instruments and across time.
    """
    return atr(high, low, close, period) / close.replace(0.0, np.nan) * 100.0
