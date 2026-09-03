"""ICT / Smart Money Concepts engine.

Looks for the mechanical footprints of institutional order flow, in the order
they normally occur:

  1. **Liquidity sweep** — a prior swing is run and immediately reclaimed. Stops
     were taken. Weighted heaviest, because it is the least ambiguous of the
     concepts and the only one with a clean pass/fail definition.
  2. **CHoCH / BOS** — structure breaks with real displacement behind them. A
     CHoCH after a sweep is the classic reversal sequence.
  3. **Order block proximity** — price returning into the last opposing candle
     before a displacement leg.
  4. **Fair value gap** — an unmitigated three-bar imbalance in the direction of
     the read.
  5. **Premium / discount** — where price sits in its dealing range. Buying in
     premium is penalised even when everything else agrees.

Everything is measured on the *entry* timeframe (the lowest in the profile) but
validated against the higher timeframe's dealing range, which is how the concept
is actually meant to be applied.
"""
from __future__ import annotations

from analyst.core.config import Settings
from analyst.core.enums import EngineId
from analyst.core.models import EngineResult, MarketContext
from analyst.engines.base import Engine, ScoreBuilder, clamp, scale
from analyst.indicators.structure import (
    detect_structure_events,
    find_fair_value_gaps,
    find_liquidity_sweeps,
    find_order_blocks,
    find_swings,
    premium_discount,
)
from analyst.indicators.trend import atr

#: How recent an event must be (in bars) to still matter.
RECENCY = {"sweep": 12, "structure": 20, "zone": 120}

#: Bars of history the structure work looks at.
#:
#: An order block or liquidity sweep from three thousand bars ago is not a level
#: anyone is trading against - it is noise that survived. Capping the window is
#: therefore more correct, not just faster. It also matters a great deal for
#: cost: swing detection and sweep matching are the most expensive functions in
#: the system, and a backtest calls them once per replayed step, so an uncapped
#: window turns a one-minute replay into a ten-minute one.
STRUCTURE_WINDOW = 800


class IctSmcEngine(Engine):
    id = EngineId.ICT_SMC

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _run(self, ctx: MarketContext) -> EngineResult:
        entry_tf = min(ctx.series, key=lambda tf: tf.minutes)
        higher_tf = max(ctx.series, key=lambda tf: tf.minutes)
        series = ctx.series[entry_tf]
        if len(series) < 150:
            return EngineResult.skipped(self.id, f"Not enough history on {entry_tf.label}")
        df = series.df.tail(STRUCTURE_WINDOW)

        atr_series = atr(df["high"], df["low"], df["close"], 14)
        price = float(df["close"].iloc[-1])
        atr_now = float(atr_series.iloc[-1])
        last_index = len(df) - 1

        builder = ScoreBuilder()
        metrics: dict[str, float] = {}

        swings = find_swings(df, 2, 2)
        events = detect_structure_events(df, atr_series)
        sweeps = find_liquidity_sweeps(df, atr_series, swings)
        obs = find_order_blocks(df, atr_series, events)
        fvgs = find_fair_value_gaps(df, atr_series)

        # 1 ------------------------------------------------------- sweep
        recent_sweeps = [s for s in sweeps if last_index - s.index <= RECENCY["sweep"]]
        if recent_sweeps:
            sweep = max(recent_sweeps, key=lambda s: s.index)
            bars_ago = last_index - sweep.index
            decay = 1.0 - bars_ago / (RECENCY["sweep"] + 1)
            value = sweep.direction * clamp(0.55 + 0.45 * scale(sweep.penetration_atr, 1.2)) * decay
            builder.add(
                "liquidity_sweep",
                "Confirmed liquidity sweep",
                value,
                1.5,
                detail=(
                    f"Level {sweep.swept_level:.4f} was run by "
                    f"{sweep.penetration_atr:.2f} ATR and reclaimed within "
                    f"{sweep.reclaim_bars} bar(s), {bars_ago} bars ago"
                ),
            )
            metrics["sweep_direction"] = float(sweep.direction)
            metrics["sweep_penetration_atr"] = sweep.penetration_atr
        else:
            builder.add("liquidity_sweep", "Liquidity sweep", 0.0, 1.5)
            builder.note("no_sweep", "No recent liquidity sweep",
                         "The setup lacks the primary ICT trigger")

        # 2 --------------------------------------------------- structure
        recent_events = [e for e in events if last_index - e.index <= RECENCY["structure"]]
        if recent_events:
            ev = max(recent_events, key=lambda e: e.index)
            bars_ago = last_index - ev.index
            base = 0.85 if ev.kind == "CHoCH" else 0.65
            value = ev.direction * clamp(base * scale(ev.displacement_atr, 2.0) + 0.15)
            value *= 1.0 - bars_ago / (RECENCY["structure"] + 1) * 0.5
            builder.add(
                f"structure_{ev.kind.lower()}",
                ("Change of character (CHoCH)" if ev.kind == "CHoCH"
                 else "Break of structure (BOS)"),
                value,
                1.2,
                detail=(
                    f"Broke {ev.level:.4f} on a {ev.displacement_atr:.2f} ATR "
                    f"impulse leg, {bars_ago} bars ago"
                ),
            )
            metrics["structure_event_direction"] = float(ev.direction)
            metrics["structure_displacement_atr"] = ev.displacement_atr
            # sweep -> CHoCH in the same direction is the textbook reversal
            if recent_sweeps and ev.kind == "CHoCH" and ev.direction == recent_sweeps[-1].direction:
                builder.add("sweep_choch_sequence", "Sequence: sweep then CHoCH",
                            ev.direction * 0.9, 0.8,
                            detail="The textbook ICT reversal sequence")
        else:
            builder.add("structure_event", "Recent structure break", 0.0, 1.2)

        # 3 ------------------------------------------------- order block
        live_obs = [
            z for z in obs
            if not z.mitigated and last_index - z.index <= RECENCY["zone"]
        ]
        ob_hit = next((z for z in reversed(live_obs) if z.contains(price)), None)
        if ob_hit is not None:
            builder.add(
                "order_block_entry", "Price inside a live order block",
                ob_hit.direction * (0.6 + 0.4 * ob_hit.strength), 1.1,
                detail=f"Zone [{ob_hit.bottom:.4f} – {ob_hit.top:.4f}], strength {ob_hit.strength:.2f}",
            )
            metrics["in_order_block"] = float(ob_hit.direction)
        elif live_obs:
            nearest = min(live_obs, key=lambda z: abs(price - z.mid))
            distance_atr = abs(price - nearest.mid) / max(atr_now, 1e-9)
            if distance_atr <= 2.0:
                builder.add(
                    "order_block_near", "Approaching an order block",
                    nearest.direction * 0.35 * (1 - distance_atr / 2.0), 0.7,
                    detail=f"{distance_atr:.2f} ATR from the zone midpoint",
                )
            metrics["nearest_ob_distance_atr"] = round(distance_atr, 3)

        # 4 ---------------------------------------------------------- FVG
        live_fvgs = [
            z for z in fvgs if not z.mitigated and last_index - z.index <= RECENCY["zone"]
        ]
        if live_fvgs:
            nearest_gap = min(live_fvgs, key=lambda z: abs(price - z.mid))
            inside = nearest_gap.contains(price)
            builder.add(
                "fair_value_gap",
                "Unmitigated fair value gap (FVG)",
                nearest_gap.direction * (0.55 if inside else 0.28) * (0.5 + 0.5 * nearest_gap.strength),
                0.8,
                detail=(
                    f"{'Price inside the gap' if inside else 'Nearby gap'} "
                    f"[{nearest_gap.bottom:.4f} – {nearest_gap.top:.4f}]"
                ),
            )
            metrics["open_fvg_count"] = float(len(live_fvgs))

        # 5 --------------------------------------------- premium/discount
        higher_df = ctx.series[higher_tf].df
        pos, label = premium_discount(higher_df, lookback=60)
        # discount favours longs (+) and premium favours shorts (-)
        pd_score = clamp((0.5 - pos) * 2.4)
        builder.add(
            "premium_discount", f"Position within the {higher_tf.label} range",
            pd_score, 0.9,
            detail=f"{label} — at {pos:.0%} of the range",
            record_when_zero=True,
        )
        metrics["range_position"] = round(pos, 4)

        quality = self._quality(len(df), bool(recent_sweeps), bool(events), atr_now)
        notes = []
        if series.derived:
            notes.append(
                f"{entry_tf.label} frame is resampled, not native from the provider"
            )
        return builder.result(self.id, quality=quality, metrics=metrics, notes=notes)

    @staticmethod
    def _quality(bars: int, has_sweep: bool, has_events: bool, atr_now: float) -> float:
        """ICT reads need history and a live tape; say so instead of pretending."""
        q = min(1.0, bars / 400.0)
        if not has_events:
            q *= 0.55          # no structure breaks at all: very little to read
        if not has_sweep:
            q *= 0.85
        if atr_now <= 0:
            return 0.0
        return round(q, 4)
