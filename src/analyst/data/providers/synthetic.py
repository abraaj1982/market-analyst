"""Deterministic synthetic price generator.

Purpose is narrow and important: the entire pipeline must be runnable and
testable with **no network and no API key**. Tests assert on exact numbers, and
a first-time user can run `analyst demo` and see a real report before wiring
anything up.

The generator is not a market simulator and makes no claim to be. It produces
regime-switching series with volatility clustering so that structure detection,
gating and reporting all exercise realistic shapes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analyst.core.clock import now_utc
from analyst.core.enums import Timeframe
from analyst.core.models import Instrument
from analyst.data.providers.base import FundamentalsProvider, PriceProvider

#: Rough starting level and volatility per symbol so demo output reads sanely.
_ANCHORS: dict[str, tuple[float, float]] = {
    "XAUUSD": (2400.0, 0.0075),
    "XAGUSD": (29.0, 0.0140),
    "EURUSD": (1.0850, 0.0040),
    "GBPUSD": (1.2700, 0.0045),
    "USDJPY": (152.0, 0.0050),
    "SPX": (5300.0, 0.0080),
    "NDX": (18500.0, 0.0100),
    "AAPL": (215.0, 0.0130),
    "NVDA": (120.0, 0.0250),
}


class SyntheticProvider(PriceProvider):
    name = "synthetic"
    native_timeframes = (
        Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.D1, Timeframe.W1,
    )

    def __init__(self, seed: int = 20240101, anchor_end: pd.Timestamp | None = None) -> None:
        self.seed = seed
        self.anchor_end = anchor_end

    def fetch(self, instrument: Instrument, timeframe: Timeframe, bars: int) -> pd.DataFrame:
        base, vol = _ANCHORS.get(instrument.symbol, (100.0, 0.01))
        # symbol-stable seed so repeated runs and repeated timeframes agree
        rng = np.random.default_rng(self.seed + (hash(instrument.symbol) % 100_000))

        n = bars + 5
        per_bar_vol = vol * np.sqrt(timeframe.minutes / 1440.0)
        returns = _regime_returns(rng, n, per_bar_vol)
        close = base * np.exp(np.cumsum(returns))
        # Anchor the *latest* bar to the reference level so demo output reads at a
        # believable price. A long random walk otherwise drifts gold to 1300 or
        # 4000, which makes the report look broken even though the maths is fine.
        close *= base / close[-1]

        # intrabar shape: open near previous close, wicks proportional to the move
        open_ = np.empty(n)
        open_[0] = close[0]
        open_[1:] = close[:-1] * (1 + rng.normal(0, per_bar_vol * 0.12, n - 1))
        body_hi = np.maximum(open_, close)
        body_lo = np.minimum(open_, close)
        wick = np.abs(returns) * close * rng.uniform(0.25, 1.1, n) + close * per_bar_vol * 0.15
        high = body_hi + wick * rng.uniform(0.2, 1.0, n)
        low = body_lo - wick * rng.uniform(0.2, 1.0, n)

        volume = np.abs(returns) / per_bar_vol * rng.uniform(6e5, 1.4e6, n)

        if self.anchor_end is not None:
            end = self.anchor_end
        elif timeframe is Timeframe.W1:
            # "1W-MON" is an anchored offset, not a fixed one -- .floor()
            # rejects it outright, so round to the most recent Monday by hand.
            now = pd.Timestamp(now_utc())
            end = now.normalize() - pd.Timedelta(days=now.weekday())
        else:
            end = pd.Timestamp(now_utc()).floor(timeframe.pandas_rule)
        idx = pd.date_range(end=end, periods=n, freq=timeframe.pandas_rule, tz="UTC")

        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=idx,
        ).tail(bars)


def _regime_returns(rng: np.random.Generator, n: int, vol: float) -> np.ndarray:
    """Markov-switching drift with GARCH-like volatility clustering."""
    states = np.array([1.0, -1.0, 0.0])          # up / down / range
    drifts = np.array([0.45, -0.45, 0.0]) * vol
    vols = np.array([1.0, 1.15, 0.65]) * vol

    out = np.empty(n)
    state = int(rng.integers(0, 3))
    sigma = vols[state]
    for i in range(n):
        if rng.random() < 0.012:                  # ~1 regime change per 80 bars
            state = int(rng.integers(0, 3))
        target = vols[state]
        sigma = 0.94 * sigma + 0.06 * target      # volatility mean-reverts slowly
        shock = rng.normal(0.0, sigma)
        sigma = np.clip(0.90 * sigma + 0.10 * abs(shock), target * 0.4, target * 3.0)
        out[i] = drifts[state] + shock
    _ = states
    return out


class SyntheticFundamentals(FundamentalsProvider):
    """Plausible, stable fundamentals so the equity engine can be exercised offline."""

    name = "synthetic_fundamentals"

    def fetch(self, instrument: Instrument) -> dict:
        rng = np.random.default_rng(abs(hash(instrument.symbol)) % 10_000)
        return {
            "trailing_pe": float(rng.uniform(12, 45)),
            "forward_pe": float(rng.uniform(10, 38)),
            "price_to_book": float(rng.uniform(1.0, 12.0)),
            "peg_ratio": float(rng.uniform(0.7, 3.2)),
            "profit_margin": float(rng.uniform(0.03, 0.35)),
            "return_on_equity": float(rng.uniform(0.05, 0.55)),
            "revenue_growth": float(rng.uniform(-0.08, 0.40)),
            "earnings_growth": float(rng.uniform(-0.15, 0.55)),
            "debt_to_equity": float(rng.uniform(10, 190)),
            "current_ratio": float(rng.uniform(0.8, 3.5)),
            "dividend_yield": float(rng.uniform(0.0, 0.05)),
            "payout_ratio": float(rng.uniform(0.0, 0.8)),
            "beta": float(rng.uniform(0.6, 2.0)),
            "market_cap": float(rng.uniform(5e10, 3e12)),
            "sector": "Technology",
            "next_earnings": None,
        }
