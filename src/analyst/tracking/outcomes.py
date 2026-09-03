"""Forward-test outcome tracking — the honesty mechanism of the whole system.

Every actionable signal is written down with its exact levels *before* the
result is known. This module walks the candles that printed afterwards and
records what actually happened: target hit, stop hit, or expired.

Two details make the numbers trustworthy rather than flattering:

  * **Stop-first within a bar.** When a single candle's range covers both the
    stop and the target, the outcome is recorded as a stop. Without intrabar
    data it is impossible to know which came first, and assuming the favourable
    order is exactly how backtests manufacture win rates that never survive
    live trading.

  * **MFE and MAE.** How far the trade ran in favour and against, in R
    multiples, regardless of the final outcome. A strategy whose winners barely
    reach 1R before turning is a different animal from one whose winners run to
    3R, and the win rate alone cannot tell them apart.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from analyst.core.clock import now_utc, to_utc
from analyst.core.enums import Timeframe
from analyst.data.repository import CandleRepository
from analyst.storage.db import session_scope
from analyst.storage.models import Signal

log = logging.getLogger(__name__)

#: A signal that has resolved neither way after this long is closed as expired.
DEFAULT_EXPIRY = timedelta(days=21)


@dataclass(slots=True)
class Outcome:
    status: str          # tp1_hit | tp2_hit | stopped | expired | open
    exit_price: float | None
    r_multiple: float | None
    max_favourable_r: float
    max_adverse_r: float
    bars_held: int


def evaluate(
    signal: Signal, candles: pd.DataFrame, expiry: timedelta = DEFAULT_EXPIRY
) -> Outcome:
    """Replay candles printed after `issued_at` and decide the outcome."""
    issued = to_utc(signal.issued_at)
    future = candles[candles.index > issued]
    risk = abs(signal.entry - signal.stop_loss)
    if risk <= 0 or future.empty:
        return Outcome("open", None, None, 0.0, 0.0, 0)

    sign = 1.0 if signal.direction > 0 else -1.0
    mfe = mae = 0.0

    for i, (ts, bar) in enumerate(future.iterrows(), start=1):
        favourable = (bar["high"] - signal.entry) * sign if sign > 0 else (signal.entry - bar["low"])
        adverse = (signal.entry - bar["low"]) * sign if sign > 0 else (bar["high"] - signal.entry)
        mfe = max(mfe, favourable / risk)
        mae = max(mae, adverse / risk)

        hit_stop = bar["low"] <= signal.stop_loss if sign > 0 else bar["high"] >= signal.stop_loss
        hit_tp1 = bar["high"] >= signal.take_profit_1 if sign > 0 else bar["low"] <= signal.take_profit_1
        hit_tp2 = (
            signal.take_profit_2 is not None
            and (bar["high"] >= signal.take_profit_2 if sign > 0 else bar["low"] <= signal.take_profit_2)
        )

        # Stop takes precedence inside a single bar — see module docstring.
        if hit_stop:
            return Outcome("stopped", float(signal.stop_loss), -1.0, mfe, max(mae, 1.0), i)
        if hit_tp2:
            r = abs(signal.take_profit_2 - signal.entry) / risk
            return Outcome("tp2_hit", float(signal.take_profit_2), r, max(mfe, r), mae, i)
        if hit_tp1:
            r = abs(signal.take_profit_1 - signal.entry) / risk
            return Outcome("tp1_hit", float(signal.take_profit_1), r, max(mfe, r), mae, i)

        if to_utc(ts) - issued > expiry:
            close = float(bar["close"])
            return Outcome("expired", close, (close - signal.entry) * sign / risk, mfe, mae, i)

    return Outcome("open", None, None, mfe, mae, len(future))


class OutcomeTracker:
    def __init__(self, repository: CandleRepository, timeframe: Timeframe = Timeframe.H1) -> None:
        self.repo = repository
        self.timeframe = timeframe

    def update_open_signals(self, expiry: timedelta = DEFAULT_EXPIRY) -> dict[str, int]:
        """Resolve every open signal it can. Returns a count per outcome."""
        counts: dict[str, int] = {}
        with session_scope() as session:
            open_rows = list(
                session.query(Signal).filter(Signal.status == "open").all()
            )
            for signal in open_rows:
                candles = self.repo.read(signal.symbol, self.timeframe, bars=5000)
                if candles.empty:
                    continue
                outcome = evaluate(signal, candles, expiry)
                signal.max_favourable_r = round(outcome.max_favourable_r, 4)
                signal.max_adverse_r = round(outcome.max_adverse_r, 4)
                signal.bars_held = outcome.bars_held
                if outcome.status != "open":
                    signal.status = outcome.status
                    signal.exit_price = outcome.exit_price
                    signal.r_multiple = (
                        round(outcome.r_multiple, 4) if outcome.r_multiple is not None else None
                    )
                    signal.closed_at = now_utc()
                counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return counts
