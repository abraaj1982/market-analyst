"""Classical technical analysis engine.

Deliberately the most conservative engine. Classical TA is where subjective
pattern-reading usually creeps in, so every component here is reduced to
something measurable:

  * **Support / resistance** — swing pivots clustered by ATR-scaled proximity.
    A level's weight is the number of touches, not an eyeball judgement.
  * **Regression channel** — the slope of a least-squares fit over the lookback,
    normalised by ATR, plus where price sits inside the channel.
  * **Fibonacci** — retracement of the last dominant leg. Only the golden pocket
    (0.618–0.786) scores, and only in the direction of that leg.
  * **Breakout / retest** — a level broken and then successfully retested, which
    is the one classical pattern with a clean mechanical definition.

No head-and-shoulders, no flags, no wedges. Those cannot be detected reliably
enough to justify a weight, and inventing them would undermine every other
number the system prints.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analyst.core.config import Settings
from analyst.core.enums import EngineId
from analyst.core.models import EngineResult, MarketContext
from analyst.engines.base import Engine, ScoreBuilder, clamp, scale
from analyst.indicators.structure import SwingKind, fib_levels, find_swings
from analyst.indicators.trend import atr


class ClassicTaEngine(Engine):
    id = EngineId.CLASSIC_TA

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _run(self, ctx: MarketContext) -> EngineResult:
        # Classical levels are read on the mid timeframe: high enough to be
        # meaningful, low enough to be actionable.
        tfs = sorted(ctx.series, key=lambda tf: tf.minutes)
        tf = tfs[len(tfs) // 2]
        df = ctx.series[tf].df
        if len(df) < 120:
            return EngineResult.skipped(self.id, f"Not enough history on {tf.label}")

        close = df["close"]
        price = float(close.iloc[-1])
        atr_series = atr(df["high"], df["low"], close, 14)
        atr_now = float(atr_series.iloc[-1])
        if atr_now <= 0:
            return EngineResult.skipped(self.id, "Zero volatility — distances cannot be measured")

        builder = ScoreBuilder()
        metrics: dict[str, float] = {}

        levels = self._cluster_levels(df, atr_now)
        # A level 40 ATR away is not support, it is a historical artefact.
        max_distance = self.settings.risk.max_level_distance_atr * atr_now
        levels = [lv for lv in levels if abs(lv[0] - price) <= max_distance]
        self._score_levels(builder, metrics, levels, price, atr_now)
        self._score_channel(builder, metrics, close, atr_now, tf.label)
        self._score_fibonacci(builder, metrics, df, price, atr_now)
        self._score_breakout_retest(builder, metrics, df, levels, atr_now)

        return builder.result(self.id, quality=min(1.0, len(df) / 300.0), metrics=metrics)

    # ------------------------------------------------------------------ #

    @staticmethod
    def _cluster_levels(df: pd.DataFrame, atr_now: float) -> list[tuple[float, int, str]]:
        """Group swing pivots into levels. Returns (price, touches, kind)."""
        swings = find_swings(df.tail(400), 3, 3)
        if not swings:
            return []
        tolerance = atr_now * 0.6
        clusters: list[list] = []
        for s in sorted(swings, key=lambda x: x.price):
            if clusters and abs(s.price - np.mean([c.price for c in clusters[-1]])) <= tolerance:
                clusters[-1].append(s)
            else:
                clusters.append([s])
        out = []
        for group in clusters:
            level = float(np.mean([g.price for g in group]))
            highs = sum(1 for g in group if g.kind is SwingKind.HIGH)
            kind = "resistance" if highs > len(group) / 2 else "support"
            out.append((level, len(group), kind))
        return out

    @staticmethod
    def _score_levels(
        builder: ScoreBuilder, metrics: dict, levels: list, price: float, atr_now: float
    ) -> None:
        if not levels:
            builder.add("levels", "Support and resistance", 0.0, 1.0)
            return

        below = [lv for lv in levels if lv[0] < price]
        above = [lv for lv in levels if lv[0] > price]
        nearest_support = max(below, key=lambda lv: lv[0]) if below else None
        nearest_resistance = min(above, key=lambda lv: lv[0]) if above else None

        score = 0.0
        details: list[str] = []
        if nearest_support:
            dist = (price - nearest_support[0]) / atr_now
            # sitting on support is bullish; strength scales with touch count
            score += clamp((1.0 - min(dist, 3.0) / 3.0) * min(nearest_support[1], 4) / 4.0)
            details.append(
                f"Support {nearest_support[0]:.4f} "
                f"({nearest_support[1]} touches, {dist:.1f} ATR away)"
            )
            metrics["support"] = round(nearest_support[0], 5)
            metrics["support_distance_atr"] = round(dist, 3)
        if nearest_resistance:
            dist = (nearest_resistance[0] - price) / atr_now
            score -= clamp((1.0 - min(dist, 3.0) / 3.0) * min(nearest_resistance[1], 4) / 4.0)
            details.append(
                f"Resistance {nearest_resistance[0]:.4f} "
                f"({nearest_resistance[1]} touches, {dist:.1f} ATR away)"
            )
            metrics["resistance"] = round(nearest_resistance[0], 5)
            metrics["resistance_distance_atr"] = round(dist, 3)

        builder.add(
            "support_resistance", "Position between support and resistance",
            clamp(score), 1.2, detail=" · ".join(details), record_when_zero=True,
        )
        metrics["levels_found"] = float(len(levels))

    @staticmethod
    def _score_channel(
        builder: ScoreBuilder, metrics: dict, close: pd.Series, atr_now: float, tf_label: str
    ) -> None:
        window = close.tail(90)
        x = np.arange(len(window), dtype=float)
        slope, intercept = np.polyfit(x, window.to_numpy(), 1)
        fitted = slope * x + intercept
        residual_std = float(np.std(window.to_numpy() - fitted))
        if residual_std <= 0:
            return

        # 0.25 ATR of net drift per bar is already a powerful trend; saturating
        # much earlier than that would make almost every trending tape read -1/+1.
        slope_score = scale(float(slope) / atr_now, 0.25)
        position = (float(window.iloc[-1]) - fitted[-1]) / (2 * residual_std)
        # Far above the regression line inside an uptrend is stretched, not strong.
        stretch_penalty = -clamp(position) * 0.35

        builder.add(
            "regression_slope", f"Regression channel slope ({tf_label})",
            slope_score, 1.0,
            detail=f"Slope {slope / atr_now:+.3f} ATR per bar over 90 bars",
        )
        builder.add(
            "channel_position", "Position inside the channel",
            stretch_penalty, 0.6,
            detail=f"Price is {position:+.2f} standard deviations from the fit",
        )
        metrics["regression_slope_atr"] = round(float(slope) / atr_now, 4)
        metrics["channel_position_sigma"] = round(float(position), 3)

    @staticmethod
    def _score_fibonacci(
        builder: ScoreBuilder, metrics: dict, df: pd.DataFrame, price: float, atr_now: float
    ) -> None:
        window = df.tail(120)
        hi_idx = int(np.argmax(window["high"].to_numpy()))
        lo_idx = int(np.argmin(window["low"].to_numpy()))
        hi = float(window["high"].iloc[hi_idx])
        lo = float(window["low"].iloc[lo_idx])
        if hi <= lo:
            return

        leg_up = hi_idx > lo_idx           # the dominant leg was upward
        levels = fib_levels(lo, hi)
        pocket_hi, pocket_lo = levels["0.618"], levels["0.786"]
        in_pocket = pocket_lo <= price <= pocket_hi

        if in_pocket:
            # a pullback into the golden pocket favours resumption of the leg
            value = 0.75 if leg_up else -0.75
            builder.add(
                "fibonacci_pocket", "Retracement into the Fibonacci golden pocket",
                value, 1.0,
                detail=(
                    f"Zone [{pocket_lo:.4f} – {pocket_hi:.4f}] of the "
                    f"{'up' if leg_up else 'down'} leg ({lo:.4f} → {hi:.4f})"
                ),
            )
        else:
            retracement = (hi - price) / (hi - lo) if leg_up else (price - lo) / (hi - lo)
            if retracement > 0.90:
                builder.add(
                    "fibonacci_invalidated", "Prior leg invalidated",
                    -0.5 if leg_up else 0.5, 0.7,
                    detail=f"{retracement:.0%} of the leg retraced — the prior structure is done",
                )
            metrics["fib_retracement"] = round(float(retracement), 3)
        metrics["fib_in_pocket"] = float(in_pocket)

    @staticmethod
    def _score_breakout_retest(
        builder: ScoreBuilder, metrics: dict, df: pd.DataFrame, levels: list, atr_now: float
    ) -> None:
        """A level broken in the last 30 bars and then retested from the far side."""
        if not levels:
            return
        recent = df.tail(30)
        closes = recent["close"].to_numpy()
        price = float(closes[-1])

        best: tuple[float, float, int] | None = None
        for level, touches, _kind in levels:
            was_below = closes[0] < level
            is_above = price > level
            if was_below == is_above:
                continue  # no side change
            direction = 1 if is_above else -1
            # a valid retest comes back within 1 ATR of the level after breaking
            distance = abs(price - level) / atr_now
            if distance > 1.2:
                continue
            strength = min(touches, 4) / 4.0 * (1.0 - distance / 1.2)
            if best is None or strength > best[0]:
                best = (strength, level, direction)

        if best is None:
            return
        strength, level, direction = best
        builder.add(
            "breakout_retest", "Level broken and now retested",
            direction * clamp(0.4 + 0.6 * strength), 1.1,
            detail=(
                f"{level:.4f} broke "
                f"{'upward' if direction > 0 else 'downward'} and is being retested"
            ),
        )
        metrics["retest_level"] = round(level, 5)
