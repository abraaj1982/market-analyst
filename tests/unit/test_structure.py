"""Market-structure primitives: pivots, breaks, zones, liquidity."""
from __future__ import annotations

import numpy as np
import pytest

from analyst.indicators.structure import (
    classify_structure,
    detect_structure_events,
    fib_levels,
    find_fair_value_gaps,
    find_liquidity_sweeps,
    find_order_blocks,
    find_swings,
    premium_discount,
)
from analyst.indicators.trend import atr
from tests.conftest import make_frame


def test_swing_confirmation_is_delayed():
    """A pivot must only be knowable `right` bars after it printed."""
    frame = make_frame([10, 11, 15, 11, 10, 9, 8, 12, 13, 14])
    swings = find_swings(frame, left=2, right=2)
    assert swings, "expected at least one pivot"
    for swing in swings:
        assert swing.confirmed_index == swing.index + 2


def test_classify_structure_uptrend_and_downtrend():
    """Rising peaks and rising troughs must read as bullish structure.

    Legs are three bars long so each trough is an unambiguous local minimum in
    the low column; a one-bar zigzag on a gapless series has no local minima at
    all, since every open equals the previous close.
    """
    up = [10, 11, 12, 11, 10.5, 12, 13, 14, 13, 12.5, 14, 15, 16, 15, 14.5, 16, 17, 18]
    down = [-x for x in up]
    assert classify_structure(find_swings(make_frame(up), 1, 1))[0] == 1
    assert classify_structure(find_swings(make_frame([100 + d for d in down]), 1, 1))[0] == -1


def test_classify_structure_reports_insufficient_data():
    direction, label = classify_structure([])
    assert direction == 0
    assert "insufficient" in label


def test_structure_events_have_displacement_not_extension():
    """Displacement measures the impulse leg, not the distance past the level.

    Measuring the latter systematically understates every break, because a level
    is by construction close to the price that breaks it.
    """
    quiet = 100 + np.random.default_rng(2).normal(0, 0.2, 150)
    impulse = 100 + np.linspace(0, 12, 40)
    frame = make_frame(np.concatenate([quiet, impulse]))
    atr_series = atr(frame["high"], frame["low"], frame["close"])

    events = detect_structure_events(frame, atr_series)
    assert events
    strongest = max(events, key=lambda e: e.displacement_atr)
    assert strongest.displacement_atr > strongest.extension_atr
    assert strongest.displacement_atr > 1.0


def test_structure_events_no_lookahead():
    rng = np.random.default_rng(4)
    frame = make_frame(100 + np.cumsum(rng.normal(0, 1, 400)))
    atr_full = atr(frame["high"], frame["low"], frame["close"])
    full = detect_structure_events(frame, atr_full)

    cut = 300
    part_frame = frame.iloc[:cut]
    atr_part = atr(part_frame["high"], part_frame["low"], part_frame["close"])
    partial = detect_structure_events(part_frame, atr_part)

    prefix = [e for e in full if e.index < cut - 2]
    assert len(partial) == len(prefix)
    for a, b in zip(partial, prefix, strict=True):
        assert (a.kind, a.direction, a.index) == (b.kind, b.direction, b.index)


def test_bullish_fair_value_gap_detected():
    # bar i-2 high must sit below bar i low for a bullish imbalance
    closes = [100] * 30 + [100, 108, 116] + [116] * 10
    frame = make_frame(closes, wick=0.001)
    atr_series = atr(frame["high"], frame["low"], frame["close"])
    gaps = find_fair_value_gaps(frame, atr_series, min_gap_atr=0.1)
    assert any(g.direction == 1 for g in gaps)


def test_order_blocks_require_displacement():
    quiet = 100 + np.random.default_rng(6).normal(0, 0.2, 180)
    impulse = 100 + np.linspace(0, 15, 40)
    frame = make_frame(np.concatenate([quiet, impulse]))
    atr_series = atr(frame["high"], frame["low"], frame["close"])
    events = detect_structure_events(frame, atr_series)

    with_displacement = find_order_blocks(frame, atr_series, events, min_displacement_atr=1.0)
    unreachable = find_order_blocks(frame, atr_series, events, min_displacement_atr=99.0)
    assert with_displacement
    assert unreachable == []


def test_liquidity_sweep_requires_reclaim():
    """Breaking a low and staying below is a breakdown, not a sweep.

    The leading flat section exists purely to warm ATR up (14 bars); without it
    every ATR value is NaN and the detector correctly refuses to measure depth.
    """
    warmup = [100.0] * 25
    # establish a swing low at 98, rally away from it, then run it and reclaim
    swept = warmup + [100, 98, 100, 102, 103, 102, 100, 97, 99, 101, 102]
    frame = make_frame(swept)
    atr_series = atr(frame["high"], frame["low"], frame["close"])
    swings = find_swings(frame, 1, 1)
    sweeps = find_liquidity_sweeps(frame, atr_series, swings, max_reclaim_bars=4,
                                   min_penetration_atr=0.0)
    assert any(s.direction == 1 for s in sweeps)

    # same setup, but price never closes back above the swept level
    breakdown = warmup + [100, 98, 100, 102, 103, 102, 100, 97, 96, 95, 94]
    frame2 = make_frame(breakdown)
    atr2 = atr(frame2["high"], frame2["low"], frame2["close"])
    sweeps2 = find_liquidity_sweeps(frame2, atr2, find_swings(frame2, 1, 1),
                                    max_reclaim_bars=2, min_penetration_atr=0.0)
    assert not any(s.direction == 1 for s in sweeps2)


def test_premium_discount_positions():
    rising = make_frame(np.linspace(100, 200, 120))
    pos, label = premium_discount(rising, lookback=60)
    assert pos > 0.9 and "Premium" in label

    falling = make_frame(np.linspace(200, 100, 120))
    pos2, label2 = premium_discount(falling, lookback=60)
    assert pos2 < 0.1 and "Discount" in label2


def test_fib_levels_ordering():
    levels = fib_levels(100.0, 200.0)
    assert levels["0.236"] > levels["0.5"] > levels["0.786"]
    assert levels["0.5"] == pytest.approx(150.0)
