"""Builds the MarketContext every engine reads.

This is the single place where I/O, resampling, quality scoring and regime
detection meet. Once a context is built it is immutable input: engines are pure
functions of it, which is what makes them individually testable and makes a
stored analysis reproducible.

Timeframe sourcing rule: fetch natively whenever the provider offers the
interval, and derive only what has to be derived (4H). Deriving a daily frame
from 1H would silently cap its history at whatever the intraday window allows —
84 days instead of years.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from analyst.core.clock import now_utc
from analyst.core.config import Settings
from analyst.core.enums import AssetClass, Market, Timeframe
from analyst.core.errors import DataUnavailableError, InsufficientDataError
from analyst.core.models import Instrument, MacroSnapshot, MarketContext, Series
from analyst.data import quality
from analyst.data.providers.base import FundamentalsProvider
from analyst.data.providers.calendar import EconomicCalendar
from analyst.data.repository import CandleRepository
from analyst.data.resample import build_timeframes, drop_forming_candle
from analyst.regime import detect as detect_regime

log = logging.getLogger(__name__)

#: How many bars each timeframe needs. EMA200 + swing history + percentile window.
DEFAULT_BARS = {
    Timeframe.M1: 1200,
    Timeframe.M5: 1200,
    Timeframe.M15: 1200,
    Timeframe.H1: 1500,
    Timeframe.H4: 900,
    Timeframe.D1: 900,
    Timeframe.W1: 400,
}

#: Which native timeframe a derived one is built from.
DERIVED_FROM = {Timeframe.H4: Timeframe.H1}


class ContextBuilder:
    def __init__(
        self,
        repository: CandleRepository,
        settings: Settings,
        fundamentals: FundamentalsProvider | None = None,
        calendar: EconomicCalendar | None = None,
        macro_loader=None,
        cot_loader=None,
    ) -> None:
        self.repo = repository
        self.settings = settings
        self.fundamentals = fundamentals
        self.calendar = calendar
        self.macro_loader = macro_loader
        self.cot_loader = cot_loader

    def build(
        self, instrument: Instrument, as_of: datetime | None = None, refresh: bool = True
    ) -> MarketContext:
        as_of = as_of or now_utc()
        wanted = [
            tf for tf in self.settings.active_profile.timeframes
            if tf in instrument.supported_timeframes
        ]
        if not wanted:
            raise InsufficientDataError(
                f"{instrument.symbol}: no overlap between the profile timeframes and what this market supports"
            )

        frames = self._load_frames(instrument, wanted, as_of, refresh)
        if not frames:
            raise DataUnavailableError(f"{instrument.symbol}: could not prepare any usable timeframe")

        report = quality.assess(frames, self.settings.data_quality, as_of)

        series = {
            tf: Series(instrument, tf, frame, derived=tf in DERIVED_FROM)
            for tf, frame in frames.items()
        }

        anchor = self._anchor_timeframe(series)
        regime, regime_metrics = detect_regime(series[anchor].df)

        extras: dict = {"regime_metrics": regime_metrics, "anchor_timeframe": anchor}
        self._attach_calendar(extras, instrument, as_of)
        self._attach_fundamentals(extras, instrument)
        self._attach_cot(extras, instrument)
        macro = self._load_macro(instrument)

        return MarketContext(
            instrument=instrument,
            as_of=as_of,
            series=series,
            quality=report,
            regime=regime,
            macro=macro,
            extras=extras,
        )

    # ------------------------------------------------------------------ #

    def _load_frames(
        self, instrument: Instrument, wanted: list[Timeframe], as_of: datetime, refresh: bool
    ) -> dict[Timeframe, object]:
        native = [tf for tf in wanted if tf not in DERIVED_FROM]
        derived = [tf for tf in wanted if tf in DERIVED_FROM]

        frames: dict = {}
        for tf in native:
            try:
                raw = self.repo.load(instrument, tf, DEFAULT_BARS[tf], refresh=refresh)
                frames[tf] = drop_forming_candle(raw, tf, as_of)
            except DataUnavailableError as exc:
                log.warning("%s", exc)

        for tf in derived:
            base_tf = DERIVED_FROM[tf]
            base = frames.get(base_tf)
            if base is None or base.empty:
                try:
                    base = self.repo.load(instrument, base_tf, DEFAULT_BARS[base_tf], refresh=refresh)
                except DataUnavailableError as exc:
                    log.warning("%s", exc)
                    continue
            built = build_timeframes(base, base_tf, [tf], as_of=as_of)
            if tf in built and not built[tf][0].empty:
                frames[tf] = built[tf][0]

        return {tf: f for tf, f in frames.items() if not f.empty}

    @staticmethod
    def _anchor_timeframe(series: dict[Timeframe, Series]) -> Timeframe:
        """Regime is read on the highest available timeframe with enough history."""
        for tf in (Timeframe.D1, Timeframe.H4, Timeframe.H1, Timeframe.W1, Timeframe.M15):
            if tf in series and len(series[tf]) >= 60:
                return tf
        return next(iter(series))

    def _attach_calendar(self, extras: dict, instrument: Instrument, as_of: datetime) -> None:
        if self.calendar is None:
            return
        # FX/metals are USD-driven; equities care about the macro tape too, just less.
        extras["calendar_events"] = self.calendar.events_between(
            as_of - timedelta(hours=2), as_of + timedelta(hours=72)
        )
        extras["calendar_blackout"] = self.calendar.in_blackout(as_of)
        extras["next_high_impact"] = self.calendar.next_high_impact(as_of)

    def _attach_fundamentals(self, extras: dict, instrument: Instrument) -> None:
        if self.fundamentals is None or not instrument.is_equity:
            return
        try:
            extras["fundamentals"] = self.fundamentals.fetch(instrument)
        except Exception as exc:
            log.warning("%s: could not fetch fundamentals (%s)", instrument.symbol, exc)

    def _attach_cot(self, extras: dict, instrument: Instrument) -> None:
        if self.cot_loader is None:
            return
        if instrument.asset_class not in (AssetClass.FX, AssetClass.METAL, AssetClass.INDEX):
            return
        try:
            frame = self.cot_loader(instrument.symbol)
            if frame is not None and not frame.empty:
                extras["cot"] = frame
        except Exception as exc:
            log.warning("%s: could not fetch COT (%s)", instrument.symbol, exc)

    def _load_macro(self, instrument: Instrument) -> MacroSnapshot:
        if self.macro_loader is None:
            return MacroSnapshot()
        if instrument.market is Market.MSX:
            return MacroSnapshot()  # US macro says little about a local Omani name
        try:
            return self.macro_loader()
        except Exception as exc:
            log.warning("Could not fetch macro data (%s)", exc)
            return MacroSnapshot()
