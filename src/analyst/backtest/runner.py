"""Historical replay.

The purpose is not to produce an impressive equity curve. It is to answer two
questions before committing months to live tracking:

  1. Does the confidence score mean anything — do higher bands actually work
     more often? (the reliability curve in `metrics.py`)
  2. How often does the system speak at all, and is that survivable?

## The lookahead problem, and what is done about it

A backtest is trivially easy to make profitable by accident. Three leaks are
guarded against explicitly:

**Point-in-time data.** At every step each timeframe is truncated to bars that
had already closed at that moment. Nothing later is visible, including the
forming bar.

**Engines that cannot be reconstructed.** Macro series, COT positioning and
company fundamentals are only available at their *current* values — there is no
free point-in-time archive of them. Running them over 2023 prices would feed
2026 knowledge into a 2023 decision. So they are **excluded from every
backtest** and named in the report, rather than quietly included to inflate the
result. This means a backtest is a *lower bound* on what the live system sees,
which is the correct direction for an error to run in.

The economic calendar is the exception, and it *is* attached. Its recurring
releases are generated from scheduling rules (first Friday, second Wednesday),
so they reconstruct exactly for any past date with no lookahead at all. Without
it the news gate would evaluate to NOT_EVALUATED on every step — and since a
gate that could not be evaluated is never a pass, every signal in the replay
would be blocked and the backtest would silently report zero trades forever.
The one caveat: irregular events listed in `calendar.yaml` (FOMC meetings) only
exist for the years actually written there, so replays of earlier years are
missing those dates.

**Outcome evaluation.** Handled by the same `tracking.outcomes.evaluate` used
live, including the stop-first rule when one bar covers both stop and target.

## What a backtest here is still not

It is one path through one history, with survivorship and regime bias intact,
and no slippage, spread or commission modelled. Treat a good result as "not
disqualified", never as "validated".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from analyst.backtest.metrics import BacktestReport, summarise
from analyst.core.clock import to_utc
from analyst.core.config import GateSpec, Settings
from analyst.core.enums import EngineId, Timeframe
from analyst.core.models import (
    Instrument,
    MacroSnapshot,
    MarketContext,
    Series,
)
from analyst.data import quality
from analyst.data.context import DEFAULT_BARS
from analyst.data.providers.calendar import EconomicCalendar
from analyst.data.repository import CandleRepository
from analyst.data.resample import resample
from analyst.pipeline import Pipeline, default_engines
from analyst.regime import detect as detect_regime
from analyst.storage.models import Signal
from analyst.tracking.outcomes import evaluate

log = logging.getLogger(__name__)

#: Engines with no free point-in-time history. Including them would leak the
#: present into the past; see the module docstring.
NON_REPLAYABLE = (EngineId.MACRO, EngineId.COT, EngineId.FUNDAMENTALS)

#: Minimum bars a timeframe needs before it is offered to the engines at all.
MIN_BARS = {Timeframe.M15: 260, Timeframe.H1: 260, Timeframe.H4: 220,
            Timeframe.D1: 220, Timeframe.W1: 120}


@dataclass(slots=True)
class BacktestConfig:
    step_bars: int = 4              # re-analyse every N bars of the entry timeframe
    expiry_days: int = 21
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    max_steps: int = 5000


class Backtester:
    def __init__(
        self,
        repository: CandleRepository,
        settings: Settings,
        gate_specs: list[GateSpec],
        calendar: EconomicCalendar | None = None,
    ) -> None:
        self.repo = repository
        self.settings = settings
        self.calendar = calendar if calendar is not None else EconomicCalendar()
        # Only the engines that can be honestly reconstructed at a past moment.
        engines = [e for e in default_engines(settings) if e.id not in NON_REPLAYABLE]
        # No ContextBuilder: replay constructs each context from stored history,
        # so the pipeline is only ever driven through `analyse_context`.
        self.pipeline = Pipeline(None, settings, gate_specs, engines=engines)

    # ------------------------------------------------------------------ #

    def run(self, instrument: Instrument, config: BacktestConfig | None = None) -> BacktestReport:
        config = config or BacktestConfig()
        frames = self._load_frames(instrument)
        if not frames:
            return BacktestReport(
                symbol=instrument.symbol,
                warnings=["Not enough stored history — run the analyser first so candles accumulate"],
                excluded_engines=[e.label for e in NON_REPLAYABLE],
            )

        entry_tf = min(frames, key=lambda tf: tf.minutes)
        timeline = self._timeline(frames[entry_tf], config)
        if len(timeline) < 10:
            return BacktestReport(
                symbol=instrument.symbol,
                warnings=[f"Period too short ({len(timeline)} steps only)"],
                excluded_engines=[e.label for e in NON_REPLAYABLE],
            )

        trades: list[dict] = []
        seen_direction: int | None = None
        steps = 0

        for ts in timeline:
            steps += 1
            ctx = self._context_at(instrument, frames, ts)
            if ctx is None:
                continue
            result = self.pipeline.analyse_context(ctx)
            if not result.is_actionable or result.risk is None:
                seen_direction = None
                continue
            # Only take the first bar of a new directional run: without this the
            # same setup is counted once per step and the sample is fabricated.
            if seen_direction == int(result.direction.value):
                continue
            seen_direction = int(result.direction.value)

            trades.append(
                self._resolve(instrument, frames[entry_tf], result, ts, config.expiry_days)
            )

        return summarise(
            symbol=instrument.symbol,
            trades=trades,
            steps=steps,
            start=timeline[0] if len(timeline) else None,
            end=timeline[-1] if len(timeline) else None,
            excluded_engines=[e.label for e in NON_REPLAYABLE],
        )

    # ------------------------------------------------------------------ #

    def _load_frames(self, instrument: Instrument) -> dict[Timeframe, pd.DataFrame]:
        """Read every stored timeframe once; slicing happens per step."""
        frames: dict[Timeframe, pd.DataFrame] = {}
        for tf in self.settings.active_profile.timeframes:
            if tf not in instrument.supported_timeframes:
                continue
            if tf is Timeframe.H4:
                base = self.repo.read(instrument.symbol, Timeframe.H1, bars=200_000)
                frame = resample(base, tf) if not base.empty else base
            else:
                frame = self.repo.read(instrument.symbol, tf, bars=200_000)
            if len(frame) >= MIN_BARS.get(tf, 200):
                frames[tf] = frame
        return frames

    @staticmethod
    def _timeline(entry_frame: pd.DataFrame, config: BacktestConfig) -> list[pd.Timestamp]:
        index = entry_frame.index
        if config.start is not None:
            index = index[index >= config.start]
        if config.end is not None:
            index = index[index <= config.end]
        # leave warm-up room at the front and outcome room at the back
        usable = index[200:-30] if len(index) > 260 else index[:0]
        return list(usable[:: max(1, config.step_bars)])[: config.max_steps]

    def _context_at(
        self, instrument: Instrument, frames: dict[Timeframe, pd.DataFrame], ts: pd.Timestamp
    ) -> MarketContext | None:
        """Build a context using only bars that had closed at `ts`.

        The window is trimmed to the same bar count the live path fetches
        (`DEFAULT_BARS`). That is a correctness requirement before it is a speed
        one: without it a replayed decision late in the history would see five
        years of bars where the live system sees two months, so the two would
        compute different EMAs and different percentiles for the same moment,
        and the backtest would stop describing the system being tested.
        """
        sliced: dict[Timeframe, pd.DataFrame] = {}
        for tf, frame in frames.items():
            window = frame.loc[frame.index <= ts].tail(DEFAULT_BARS.get(tf, 1500))
            if len(window) < MIN_BARS.get(tf, 200):
                continue
            sliced[tf] = window
        if not sliced:
            return None

        as_of = to_utc(ts)
        report = quality.assess(sliced, self.settings.data_quality, as_of)
        series = {tf: Series(instrument, tf, frame) for tf, frame in sliced.items()}
        anchor = max(series, key=lambda tf: tf.minutes)
        regime, regime_metrics = detect_regime(series[anchor].df)

        extras = {"regime_metrics": regime_metrics, "anchor_timeframe": anchor}
        # Rule-generated calendar events reconstruct exactly for any past date,
        # so the news gate is evaluated in the replay just as it is live.
        extras["calendar_events"] = self.calendar.events_between(
            as_of - timedelta(hours=2), as_of + timedelta(hours=72)
        )
        extras["calendar_blackout"] = self.calendar.in_blackout(as_of)
        extras["next_high_impact"] = self.calendar.next_high_impact(as_of)

        return MarketContext(
            instrument=instrument,
            as_of=as_of,
            series=series,
            quality=report,
            regime=regime,
            macro=MacroSnapshot(),          # deliberately empty; see module docstring
            extras=extras,
        )

    @staticmethod
    def _resolve(
        instrument: Instrument,
        entry_frame: pd.DataFrame,
        result,
        ts: pd.Timestamp,
        expiry_days: int,
    ) -> dict:
        plan = result.risk
        signal = Signal(
            analysis_id=0, symbol=instrument.symbol, issued_at=to_utc(ts),
            direction=int(result.direction.value), grade=result.grade.value,
            confidence=result.confidence, entry=plan.entry, stop_loss=plan.stop_loss,
            take_profit_1=plan.take_profit_1, take_profit_2=plan.take_profit_2,
            risk_reward=plan.risk_reward, status="open",
        )
        outcome = evaluate(signal, entry_frame, expiry=timedelta(days=expiry_days))
        return {
            "issued_at": to_utc(ts),
            "direction": int(result.direction.value),
            "grade": result.grade.value,
            "confidence": result.confidence,
            "entry": plan.entry,
            "stop_loss": plan.stop_loss,
            "status": outcome.status,
            "r_multiple": outcome.r_multiple,
            "max_favourable_r": outcome.max_favourable_r,
            "max_adverse_r": outcome.max_adverse_r,
            "bars_held": outcome.bars_held,
        }
