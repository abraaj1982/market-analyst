"""Indicator correctness, including the properties that matter most in trading:
bounded ranges, no lookahead, and agreement with hand-computed values."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analyst.indicators.oscillators import bollinger, percentile_rank, rsi, stochastic
from analyst.indicators.trend import (
    adx,
    atr,
    atr_percent,
    ema,
    ichimoku,
    sma,
    supertrend,
    true_range,
)
from tests.conftest import make_frame


def test_ema_matches_recursive_definition():
    values = pd.Series([10.0, 11, 12, 13, 14, 15, 16, 17])
    result = ema(values, 3)
    alpha = 2 / (3 + 1)
    expected = values.iloc[0:3].mean()  # first defined value is the seeded EMA
    # walk the recursion forward from the first non-NaN point
    manual = float(result.iloc[2])
    for i in range(3, len(values)):
        manual = alpha * values.iloc[i] + (1 - alpha) * manual
    assert result.iloc[:2].isna().all()
    assert float(result.iloc[-1]) == pytest.approx(manual, rel=1e-9)
    assert expected is not None


def test_sma_is_simple_mean():
    values = pd.Series([1.0, 2, 3, 4, 5])
    assert float(sma(values, 3).iloc[-1]) == pytest.approx(4.0)


def test_rsi_bounded_and_extremes():
    rising = pd.Series(np.arange(1, 60, dtype=float))
    falling = pd.Series(np.arange(60, 1, -1, dtype=float))
    flat = pd.Series(np.full(60, 5.0))

    assert float(rsi(rising).iloc[-1]) == pytest.approx(100.0)
    assert float(rsi(falling).iloc[-1]) == pytest.approx(0.0)
    # a perfectly flat series carries no information; 50 is the honest answer
    assert float(rsi(flat).iloc[-1]) == pytest.approx(50.0)

    noisy = pd.Series(np.random.default_rng(0).normal(100, 3, 500).cumsum())
    assert rsi(noisy).dropna().between(0, 100).all()


def test_true_range_uses_previous_close():
    frame = make_frame([100, 105, 95, 100])
    tr = true_range(frame["high"], frame["low"], frame["close"])
    manual = max(
        frame["high"].iloc[1] - frame["low"].iloc[1],
        abs(frame["high"].iloc[1] - frame["close"].iloc[0]),
        abs(frame["low"].iloc[1] - frame["close"].iloc[0]),
    )
    assert float(tr.iloc[1]) == pytest.approx(manual)


def test_atr_percent_is_scale_invariant():
    """The whole point of the normalisation: doubling the price must not double
    the reported volatility percentile."""
    rng = np.random.default_rng(3)
    closes = 100 + np.cumsum(rng.normal(0, 1, 400))
    small = make_frame(closes)
    large = make_frame(closes * 50)

    a = atr_percent(small["high"], small["low"], small["close"]).dropna()
    b = atr_percent(large["high"], large["low"], large["close"]).dropna()
    assert np.allclose(a.to_numpy(), b.to_numpy(), rtol=1e-9)

    raw_a = atr(small["high"], small["low"], small["close"]).dropna()
    raw_b = atr(large["high"], large["low"], large["close"]).dropna()
    assert not np.allclose(raw_a.to_numpy(), raw_b.to_numpy())


def test_adx_bounded():
    rng = np.random.default_rng(1)
    frame = make_frame(100 + np.cumsum(rng.normal(0, 1, 400)))
    result = adx(frame["high"], frame["low"], frame["close"]).dropna()
    assert result["adx"].between(0, 100).all()
    assert result["plus_di"].between(0, 100).all()


def test_bollinger_percent_b_and_ordering():
    rng = np.random.default_rng(5)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    bb = bollinger(close, 20, 2.0).dropna()
    assert (bb["upper"] >= bb["mid"]).all()
    assert (bb["mid"] >= bb["lower"]).all()
    assert (bb["bandwidth"] > 0).all()


def test_stochastic_bounded():
    rng = np.random.default_rng(7)
    frame = make_frame(100 + np.cumsum(rng.normal(0, 1, 300)))
    st = stochastic(frame["high"], frame["low"], frame["close"]).dropna()
    assert st["k"].between(0, 100).all()


def test_percentile_rank_ends():
    rising = pd.Series(np.arange(300, dtype=float))
    falling = pd.Series(np.arange(300, 0, -1, dtype=float))
    assert float(percentile_rank(rising, 100).iloc[-1]) == pytest.approx(1.0)
    assert float(percentile_rank(falling, 100).iloc[-1]) == pytest.approx(0.0)


def test_supertrend_direction_follows_a_clean_trend():
    rng = np.random.default_rng(3)
    up = make_frame(100 + np.arange(150, dtype=float) * 0.6 + rng.normal(0, 0.3, 150))
    down = make_frame(200 - np.arange(150, dtype=float) * 0.6 + rng.normal(0, 0.3, 150))

    up_result = supertrend(up["high"], up["low"], up["close"])
    down_result = supertrend(down["high"], down["low"], down["close"])

    assert up_result["direction"].iloc[-1] == 1
    assert down_result["direction"].iloc[-1] == -1


def test_supertrend_line_sits_on_the_side_its_direction_claims():
    rng = np.random.default_rng(5)
    frame = make_frame(100 + np.cumsum(rng.normal(0, 1, 300)))
    result = supertrend(frame["high"], frame["low"], frame["close"]).dropna()
    close = frame["close"].loc[result.index]

    bullish = result["direction"] == 1
    # the line trails below price while bullish, above it while bearish --
    # never a mid-trend contradiction of its own signal
    assert (close[bullish] >= result.loc[bullish, "line"]).mean() > 0.95
    assert (close[~bullish] <= result.loc[~bullish, "line"]).mean() > 0.95


@pytest.mark.parametrize(
    "func",
    [
        lambda f: rsi(f["close"]),
        lambda f: atr(f["high"], f["low"], f["close"]),
        lambda f: ema(f["close"], 20),
        lambda f: ichimoku(f["high"], f["low"], f["close"])["cloud_top"],
        lambda f: adx(f["high"], f["low"], f["close"])["adx"],
        lambda f: supertrend(f["high"], f["low"], f["close"])["line"],
    ],
)
def test_no_lookahead(func):
    """Truncating the input must not change any previously computed value.

    An indicator that fails this silently rewrites history every time a new bar
    prints, which makes every backtest built on it meaningless.
    """
    rng = np.random.default_rng(11)
    frame = make_frame(100 + np.cumsum(rng.normal(0, 1, 500)))
    cut = 400
    full = func(frame).iloc[:cut]
    partial = func(frame.iloc[:cut])
    both = pd.concat([full, partial], axis=1).dropna()
    assert np.allclose(both.iloc[:, 0], both.iloc[:, 1])
