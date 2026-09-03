"""Price-structure primitives: swings, BOS/CHoCH, order blocks, FVGs, liquidity.

These are the building blocks the ICT/SMC engine reasons over. Two rules hold
throughout:

1. **No lookahead.** A swing pivot at bar *i* is only *confirmed* once `right`
   bars have printed after it. Every function returns pivots with a
   `confirmed_at` index, and consumers must respect it. Detecting a pivot the
   instant it forms is the classic way backtests invent profits that never
   existed live.

2. **Everything is measurable.** Each detected object carries the numbers that
   justify it (displacement size, gap width, sweep depth), so the report can
   show evidence rather than assertions.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class SwingKind(str, Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Swing:
    kind: SwingKind
    index: int            # positional index of the pivot bar
    ts: pd.Timestamp
    price: float
    confirmed_index: int  # first bar at which this pivot was knowable


@dataclass(frozen=True, slots=True)
class StructureEvent:
    """A break of structure (BOS) or change of character (CHoCH).

    Two different magnitudes are recorded because they answer different
    questions:

    * `displacement_atr` — the size of the impulsive leg that produced the
      break, measured from the leg's origin to the breaking close. This is what
      ICT calls displacement and what makes an order block worth respecting.
    * `extension_atr` — how far beyond the broken level price actually closed.
      A large displacement with a tiny extension is a break that barely cleared
      the level: still a break, but a weaker one.

    An earlier version of this module used the extension as the displacement.
    That systematically under-measured every break, because a level is by
    construction close to the price that breaks it.
    """

    kind: str             # "BOS" | "CHoCH"
    direction: int        # +1 bullish, -1 bearish
    index: int
    ts: pd.Timestamp
    level: float          # the swing level that was broken
    close: float
    displacement_atr: float
    extension_atr: float = 0.0


@dataclass(frozen=True, slots=True)
class Zone:
    """An order block or fair-value gap: a price band with provenance."""

    kind: str             # "order_block" | "fvg"
    direction: int        # +1 demand/bullish, -1 supply/bearish
    top: float
    bottom: float
    index: int
    ts: pd.Timestamp
    strength: float       # 0..1, from displacement / gap size
    mitigated: bool = False

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


@dataclass(frozen=True, slots=True)
class LiquiditySweep:
    """Price ran a prior swing then closed back inside — a stop raid."""

    direction: int        # +1 = swept lows then reversed up (bullish)
    index: int
    ts: pd.Timestamp
    swept_level: float
    penetration_atr: float
    reclaim_bars: int


# --------------------------------------------------------------------------- #
# Swings
# --------------------------------------------------------------------------- #


def find_swings(df: pd.DataFrame, left: int = 2, right: int = 2) -> list[Swing]:
    """Fractal pivots: a high with `left` lower highs before and `right` after.

    `right` bars of delay is the price of honesty — the pivot is only confirmed
    at `index + right`.
    """
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    ts = df.index
    out: list[Swing] = []
    for i in range(left, len(df) - right):
        window_h = highs[i - left : i + right + 1]
        window_l = lows[i - left : i + right + 1]
        if highs[i] == window_h.max() and (window_h.argmax() == left):
            out.append(Swing(SwingKind.HIGH, i, ts[i], float(highs[i]), i + right))
        elif lows[i] == window_l.min() and (window_l.argmin() == left):
            out.append(Swing(SwingKind.LOW, i, ts[i], float(lows[i]), i + right))
    return out


def confirmed_swings(swings: list[Swing], as_of_index: int) -> list[Swing]:
    """Only the pivots that were already knowable at `as_of_index`."""
    return [s for s in swings if s.confirmed_index <= as_of_index]


def classify_structure(swings: list[Swing], lookback: int = 6) -> tuple[int, str]:
    """Return (direction, arabic label) from the last few alternating pivots.

    Bullish structure = higher highs AND higher lows. Anything mixed is a range,
    which is information, not a failure.
    """
    highs = [s for s in swings if s.kind is SwingKind.HIGH][-lookback:]
    lows = [s for s in swings if s.kind is SwingKind.LOW][-lookback:]
    if len(highs) < 2 or len(lows) < 2:
        return 0, "بنية غير كافية"

    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price

    if hh and hl:
        return 1, "قمم وقيعان صاعدة (HH/HL)"
    if lh and ll:
        return -1, "قمم وقيعان هابطة (LH/LL)"
    if hh and ll:
        return 0, "توسّع نطاق (قمة أعلى وقاع أدنى)"
    return 0, "نطاق عرضي / بنية مختلطة"


# --------------------------------------------------------------------------- #
# BOS / CHoCH
# --------------------------------------------------------------------------- #


def detect_structure_events(
    df: pd.DataFrame,
    atr_series: pd.Series,
    left: int = 2,
    right: int = 2,
    impulse_lookback: int = 10,
) -> list[StructureEvent]:
    """Walk the bars forward, tracking the last confirmed swing high/low.

    A close beyond the most recent confirmed opposite swing is a break. It is a
    BOS when it continues the prevailing direction, and a CHoCH when it reverses
    it — the distinction that separates trend continuation from trend failure.
    """
    swings = find_swings(df, left, right)
    if not swings:
        return []

    closes = df["close"].to_numpy()
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    atr_arr = atr_series.to_numpy()
    events: list[StructureEvent] = []
    bias = 0

    last_high: Swing | None = None
    last_low: Swing | None = None
    by_confirm: dict[int, list[Swing]] = {}
    for s in swings:
        by_confirm.setdefault(s.confirmed_index, []).append(s)

    for i in range(len(df)):
        for s in by_confirm.get(i, []):
            if s.kind is SwingKind.HIGH:
                last_high = s
            else:
                last_low = s

        atr_i = atr_arr[i] if i < len(atr_arr) and atr_arr[i] > 0 else np.nan
        if np.isnan(atr_i):
            continue

        origin = max(0, i - impulse_lookback)

        if last_high is not None and closes[i] > last_high.price:
            kind = "CHoCH" if bias < 0 else "BOS"
            leg = closes[i] - lows[origin : i + 1].min()
            events.append(
                StructureEvent(
                    kind, 1, i, df.index[i], last_high.price, float(closes[i]),
                    float(leg / atr_i),
                    float((closes[i] - last_high.price) / atr_i),
                )
            )
            bias = 1
            last_high = None  # consumed; wait for the next confirmed pivot
        elif last_low is not None and closes[i] < last_low.price:
            kind = "CHoCH" if bias > 0 else "BOS"
            leg = highs[origin : i + 1].max() - closes[i]
            events.append(
                StructureEvent(
                    kind, -1, i, df.index[i], last_low.price, float(closes[i]),
                    float(leg / atr_i),
                    float((last_low.price - closes[i]) / atr_i),
                )
            )
            bias = -1
            last_low = None
    return events


# --------------------------------------------------------------------------- #
# Fair value gaps
# --------------------------------------------------------------------------- #


def find_fair_value_gaps(
    df: pd.DataFrame, atr_series: pd.Series, min_gap_atr: float = 0.25, lookback: int = 200
) -> list[Zone]:
    """Three-bar imbalance: bar1.high < bar3.low (bullish) or the mirror.

    Only gaps wider than `min_gap_atr` survive — sub-noise gaps are everywhere
    and mean nothing.
    """
    start = max(2, len(df) - lookback)
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    atr_arr = atr_series.to_numpy()
    zones: list[Zone] = []

    for i in range(start, len(df)):
        atr_i = atr_arr[i] if atr_arr[i] > 0 else np.nan
        if np.isnan(atr_i):
            continue
        # bullish FVG between bar i-2 high and bar i low
        if lows[i] > highs[i - 2]:
            gap = lows[i] - highs[i - 2]
            if gap >= min_gap_atr * atr_i:
                zones.append(
                    Zone("fvg", 1, float(lows[i]), float(highs[i - 2]), i - 1,
                         df.index[i - 1], float(min(1.0, gap / (2 * atr_i))))
                )
        elif highs[i] < lows[i - 2]:
            gap = lows[i - 2] - highs[i]
            if gap >= min_gap_atr * atr_i:
                zones.append(
                    Zone("fvg", -1, float(lows[i - 2]), float(highs[i]), i - 1,
                         df.index[i - 1], float(min(1.0, gap / (2 * atr_i))))
                )
    return _mark_mitigated(zones, df)


# --------------------------------------------------------------------------- #
# Order blocks
# --------------------------------------------------------------------------- #


def find_order_blocks(
    df: pd.DataFrame,
    atr_series: pd.Series,
    events: list[StructureEvent],
    min_displacement_atr: float = 1.0,
) -> list[Zone]:
    """The last opposing candle before a displacement leg that broke structure.

    Anchoring order blocks to an actual structure break (rather than to any
    engulfing candle) is what keeps the count small and the zones meaningful.
    """
    opens, closes = df["open"].to_numpy(), df["close"].to_numpy()
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    atr_arr = atr_series.to_numpy()
    zones: list[Zone] = []

    for ev in events:
        if ev.displacement_atr < min_displacement_atr:
            continue
        # walk back from the break to the last candle against the break direction
        for j in range(ev.index, max(ev.index - 12, 0) - 1, -1):
            is_down_candle = closes[j] < opens[j]
            is_up_candle = closes[j] > opens[j]
            if (ev.direction > 0 and is_down_candle) or (ev.direction < 0 and is_up_candle):
                atr_j = atr_arr[j] if atr_arr[j] > 0 else np.nan
                if np.isnan(atr_j):
                    break
                zones.append(
                    Zone(
                        "order_block", ev.direction,
                        float(highs[j]), float(lows[j]), j, df.index[j],
                        float(min(1.0, ev.displacement_atr / 3.0)),
                    )
                )
                break
    return _mark_mitigated(zones, df)


def _mark_mitigated(zones: list[Zone], df: pd.DataFrame) -> list[Zone]:
    """A zone is mitigated once price has traded back through it after creation."""
    highs, lows = df["high"].to_numpy(), df["low"].to_numpy()
    out: list[Zone] = []
    for z in zones:
        after_hi = highs[z.index + 1 :]
        after_lo = lows[z.index + 1 :]
        touched = bool(len(after_hi)) and bool(
            ((after_lo <= z.top) & (after_hi >= z.bottom)).any()
        )
        out.append(
            Zone(z.kind, z.direction, z.top, z.bottom, z.index, z.ts, z.strength, touched)
        )
    return out


# --------------------------------------------------------------------------- #
# Liquidity
# --------------------------------------------------------------------------- #


def find_liquidity_sweeps(
    df: pd.DataFrame,
    atr_series: pd.Series,
    swings: list[Swing],
    max_reclaim_bars: int = 3,
    min_penetration_atr: float = 0.05,
) -> list[LiquiditySweep]:
    """Wick beyond a confirmed swing, then a close back inside within N bars.

    This is the mechanical definition of a stop raid: liquidity was taken and
    immediately rejected. Without the reclaim requirement it is just a breakout.
    """
    highs, lows, closes = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    atr_arr = atr_series.to_numpy()
    sweeps: list[LiquiditySweep] = []

    for s in swings:
        for i in range(s.confirmed_index + 1, min(len(df), s.confirmed_index + 200)):
            atr_i = atr_arr[i] if atr_arr[i] > 0 else np.nan
            if np.isnan(atr_i):
                continue
            if s.kind is SwingKind.LOW and lows[i] < s.price:
                depth = (s.price - lows[i]) / atr_i
                if depth < min_penetration_atr:
                    continue
                for k in range(i, min(i + max_reclaim_bars + 1, len(df))):
                    if closes[k] > s.price:
                        sweeps.append(
                            LiquiditySweep(1, k, df.index[k], s.price, float(depth), k - i)
                        )
                        break
                break
            if s.kind is SwingKind.HIGH and highs[i] > s.price:
                depth = (highs[i] - s.price) / atr_i
                if depth < min_penetration_atr:
                    continue
                for k in range(i, min(i + max_reclaim_bars + 1, len(df))):
                    if closes[k] < s.price:
                        sweeps.append(
                            LiquiditySweep(-1, k, df.index[k], s.price, float(depth), k - i)
                        )
                        break
                break
    return sweeps


def premium_discount(df: pd.DataFrame, lookback: int = 60) -> tuple[float, str]:
    """Where price sits inside its recent dealing range, as a 0..1 fraction.

    < 0.5 is discount (favourable for longs), > 0.5 premium (favourable for
    shorts). The equilibrium band around 0.5 is where ICT says to do nothing.
    """
    window = df.tail(lookback)
    hi, lo = float(window["high"].max()), float(window["low"].min())
    if hi <= lo:
        return 0.5, "نطاق غير صالح"
    pos = (float(window["close"].iloc[-1]) - lo) / (hi - lo)
    if pos < 0.382:
        label = "منطقة خصم (Discount) — مواتية للشراء"
    elif pos > 0.618:
        label = "منطقة علاوة (Premium) — مواتية للبيع"
    else:
        label = "منطقة توازن (Equilibrium)"
    return float(pos), label


def fib_levels(low: float, high: float) -> dict[str, float]:
    """Retracement levels for a leg. 0.618–0.786 is the golden pocket."""
    span = high - low
    return {name: high - span * r for name, r in
            (("0.236", 0.236), ("0.382", 0.382), ("0.5", 0.5),
             ("0.618", 0.618), ("0.705", 0.705), ("0.786", 0.786))}
