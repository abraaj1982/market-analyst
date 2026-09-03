"""Momentum oscillators and volatility bands."""
from __future__ import annotations

import numpy as np
import pandas as pd

from analyst.indicators.trend import ema, sma, wilder_smooth


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Flat markets yield 50 rather than NaN/100."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = wilder_smooth(gain, period)
    avg_loss = wilder_smooth(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 with gains -> 100 ; both zero -> 50 (no information)
    out = out.where(avg_loss != 0.0, np.where(avg_gain > 0.0, 100.0, 50.0))
    return out


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line}
    )


def bollinger(close: pd.Series, period: int = 20, k: float = 2.0) -> pd.DataFrame:
    """Bollinger Bands with population std (ddof=0), matching charting platforms.

    `percent_b` places price inside the band (0 = lower, 1 = upper) and
    `bandwidth` measures squeeze/expansion — the more useful of the two for
    regime work.
    """
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper, lower = mid + k * std, mid - k * std
    width = (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {
            "mid": mid,
            "upper": upper,
            "lower": lower,
            "percent_b": (close - lower) / width,
            "bandwidth": width / mid.replace(0.0, np.nan),
        }
    )


def stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series, k_period: int = 14, d_period: int = 3
) -> pd.DataFrame:
    lowest = low.rolling(k_period, min_periods=k_period).min()
    highest = high.rolling(k_period, min_periods=k_period).max()
    k = 100.0 * (close - lowest) / (highest - lowest).replace(0.0, np.nan)
    return pd.DataFrame({"k": k, "d": k.rolling(d_period, min_periods=d_period).mean()})


def rate_of_change(close: pd.Series, period: int = 20) -> pd.Series:
    return 100.0 * (close / close.shift(period) - 1.0)


def percentile_rank(series: pd.Series, window: int = 252) -> pd.Series:
    """Rolling percentile of the latest value within its own recent history.

    Used to answer "is today's volatility high *for this instrument*", which is
    the only comparison that means anything across asset classes.
    """
    def _rank(window_values: np.ndarray) -> float:
        last = window_values[-1]
        return float((window_values <= last).sum() - 1) / float(len(window_values) - 1)

    return series.rolling(window, min_periods=max(20, window // 5)).apply(_rank, raw=True)
