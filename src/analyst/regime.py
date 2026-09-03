"""Market regime detection.

The same setup does not deserve the same weight in every market condition. A
moving-average cross is worth a lot in a trend and close to nothing in a range;
an oscillator reading is the opposite. Rather than pretend one weighting fits
all, the pipeline classifies the environment first and lets the aggregator swap
weight profiles accordingly.

Classification uses three orthogonal measures on the anchor timeframe:

  * **ADX** — trend strength
  * **ATR percentile** — where current volatility sits in its own 1-year history
  * **Bollinger bandwidth percentile** — expansion versus squeeze
"""
from __future__ import annotations

import pandas as pd

from analyst.core.enums import Regime
from analyst.indicators.oscillators import bollinger, percentile_rank
from analyst.indicators.trend import adx, atr_percent


def detect(frame: pd.DataFrame, adx_period: int = 14) -> tuple[Regime, dict[str, float]]:
    """Return the regime plus the metrics that justified it."""
    if len(frame) < 60:
        return Regime.RANGING, {}

    high, low, close = frame["high"], frame["low"], frame["close"]
    adx_frame = adx(high, low, close, adx_period)
    adx_now = float(adx_frame["adx"].iloc[-1])

    # percentile of ATR-as-%-of-price, never of raw ATR (see atr_percent docstring)
    atr_series = atr_percent(high, low, close, 14)
    atr_pct = percentile_rank(atr_series, min(252, max(60, len(frame) // 2)))
    atr_rank = float(atr_pct.iloc[-1]) if pd.notna(atr_pct.iloc[-1]) else 0.5

    bb = bollinger(close, 20, 2.0)
    bw_pct = percentile_rank(bb["bandwidth"], min(252, max(60, len(frame) // 2)))
    bw_rank = float(bw_pct.iloc[-1]) if pd.notna(bw_pct.iloc[-1]) else 0.5

    metrics = {
        "adx": round(adx_now, 2),
        "atr_percentile": round(atr_rank, 3),
        "bandwidth_percentile": round(bw_rank, 3),
    }

    # Order matters: an explosive tape is "high volatility" even if ADX is high,
    # because that is the condition where structure levels get run through.
    if atr_rank >= 0.90 or bw_rank >= 0.92:
        return Regime.HIGH_VOLATILITY, metrics

    # ADX is the trend measure. Volatility vetoes it only when the tape is dead
    # on BOTH counts - range and bandwidth in their bottom deciles. Requiring
    # mid-range ATR instead would misclassify the most tradeable condition there
    # is, a steady low-volatility trend, as a range.
    dead_tape = atr_rank <= 0.10 and bw_rank <= 0.15
    if adx_now >= 25.0 and not dead_tape:
        return Regime.TRENDING, metrics
    if atr_rank <= 0.20 and bw_rank <= 0.25:
        return Regime.QUIET, metrics
    return Regime.RANGING, metrics
