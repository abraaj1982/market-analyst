"""Risk plan construction: entry, stop, targets, R:R and position sizing.

The stop is structural first and volatility-based second. Placing it purely at
`k x ATR` ignores where the trade is actually wrong; placing it purely at the
last swing ignores that noise can reach it. So the stop goes behind the relevant
swing *plus* an ATR buffer, and an ATR floor prevents an absurdly tight stop when
the swing happens to be one tick away.

Targets are expressed in R multiples rather than at arbitrary levels, because R
is the only unit in which outcomes can be compared across instruments — and it
is the unit the outcome tracker records months later.
"""
from __future__ import annotations

import pandas as pd

from analyst.core.config import RiskSettings
from analyst.core.enums import Direction, Timeframe
from analyst.core.models import MarketContext, RiskPlan
from analyst.indicators.structure import SwingKind, find_swings
from analyst.indicators.trend import atr


def build_plan(
    ctx: MarketContext, direction: Direction, settings: RiskSettings
) -> RiskPlan | None:
    """Construct a plan, or None when no defensible stop placement exists."""
    if direction is Direction.NEUTRAL:
        return None

    tf = _plan_timeframe(ctx)
    df = ctx.series[tf].df
    if len(df) < 60:
        return None

    entry = float(df["close"].iloc[-1])
    atr_now = float(atr(df["high"], df["low"], df["close"], settings.atr_period).iloc[-1])
    if atr_now <= 0:
        return None

    stop, basis = _stop_level(df, entry, atr_now, direction, settings)
    if stop is None:
        return None

    distance = abs(entry - stop)
    if distance <= 0:
        return None

    sign = float(direction.value)
    tp1 = entry + sign * distance * settings.target_r_multiple_1
    tp2 = entry + sign * distance * settings.target_r_multiple_2

    return RiskPlan(
        entry=round(entry, 8),
        stop_loss=round(stop, 8),
        take_profit_1=round(tp1, 8),
        take_profit_2=round(tp2, 8),
        risk_reward=round(settings.target_r_multiple_1, 3),
        stop_distance=round(distance, 8),
        atr=round(atr_now, 8),
        basis=basis,
        position_size_hint=_size_hint(distance, entry, settings),
    )


def _plan_timeframe(ctx: MarketContext) -> Timeframe:
    """Levels come from the entry timeframe — the lowest one in the profile."""
    return min(ctx.series, key=lambda tf: tf.minutes)


def _stop_level(
    df: pd.DataFrame,
    entry: float,
    atr_now: float,
    direction: Direction,
    settings: RiskSettings,
) -> tuple[float | None, str]:
    swings = find_swings(df.tail(120), 2, 2)
    buffer = atr_now * settings.structure_stop_buffer_atr
    atr_floor = atr_now * settings.atr_stop_multiplier
    atr_ceiling = atr_now * settings.max_stop_atr

    if direction is Direction.BULLISH:
        lows = [s.price for s in swings if s.kind is SwingKind.LOW and s.price < entry]
        structural = (max(lows) - buffer) if lows else None
        volatility = entry - atr_floor
        if structural is None:
            return volatility, f"Volatility stop: {settings.atr_stop_multiplier}x ATR below entry"
        # never tighter than the ATR floor...
        stop = min(structural, volatility)
        basis = (
            f"Stop behind the last structural low at {max(lows):.5f}, "
            f"plus a {settings.structure_stop_buffer_atr}x ATR buffer"
            if stop == structural
            else f"Volatility stop ({settings.atr_stop_multiplier}x ATR) — the structural low is too close"
        )
        # ...and never wider than the ceiling. A stop 4+ ATR away means the real
        # invalidation level is too far for the trade to have workable geometry;
        # capping it is stated openly rather than passed off as structural.
        if entry - stop > atr_ceiling:
            stop = entry - atr_ceiling
            basis = (
                f"⚠️ Structural invalidation sits beyond {settings.max_stop_atr}x ATR — "
                "the stop was capped, so it is NOT at the true invalidation level"
            )
        return stop, basis

    highs = [s.price for s in swings if s.kind is SwingKind.HIGH and s.price > entry]
    structural = (min(highs) + buffer) if highs else None
    volatility = entry + atr_floor
    if structural is None:
        return volatility, f"Volatility stop: {settings.atr_stop_multiplier}x ATR above entry"
    stop = max(structural, volatility)
    basis = (
        f"Stop behind the last structural high at {min(highs):.5f}, "
        f"plus a {settings.structure_stop_buffer_atr}x ATR buffer"
        if stop == structural
        else f"Volatility stop ({settings.atr_stop_multiplier}x ATR) — the structural high is too close"
    )
    if stop - entry > atr_ceiling:
        stop = entry + atr_ceiling
        basis = (
            f"⚠️ Structural invalidation sits beyond {settings.max_stop_atr}x ATR — "
            "the stop was capped, so it is NOT at the true invalidation level"
        )
    return stop, basis


def _size_hint(distance: float, entry: float, settings: RiskSettings) -> str:
    """Position size as a share of account equity, not a currency amount.

    A currency figure would require knowing the account size, the instrument's
    contract specification and the broker's margin rules. Expressing it as
    "risk R% of equity, which is X% of price" is exact, portable, and cannot be
    silently wrong.
    """
    pct_move = distance / entry * 100.0
    units_per_1pct = settings.account_risk_percent / pct_move if pct_move > 0 else 0.0
    return (
        f"Risk {settings.account_risk_percent:.1f}% of equity per trade · "
        f"stop distance {pct_move:.2f}% of price · "
        f"size = {units_per_1pct:.2f}x equity (adjust for your broker's leverage)"
    )
