"""Application wiring and the run loop.

One place builds the object graph, so the CLI, the scheduler and the web API can
never drift out of sync with each other. `AnalystService.run_once()` is the unit
of work: analyse the watchlist, persist, update outcomes, alert.
"""
from __future__ import annotations

import logging
from datetime import datetime

from analyst.alerts.dedupe import should_alert
from analyst.alerts.telegram import TelegramNotifier
from analyst.core.config import (
    Secrets,
    Settings,
    active_instruments,
    load_gates,
    load_settings,
    load_watchlist,
)
from analyst.core.enums import Timeframe
from analyst.core.models import AnalysisResult, Instrument
from analyst.data.context import ContextBuilder
from analyst.data.macro import MacroLoader
from analyst.data.providers.calendar import EconomicCalendar
from analyst.data.providers.cot import CotProvider
from analyst.data.providers.stooq import StooqProvider
from analyst.data.providers.synthetic import SyntheticFundamentals, SyntheticProvider
from analyst.data.providers.yahoo import YahooFundamentals, YahooProvider
from analyst.data.repository import CandleRepository
from analyst.pipeline import Pipeline
from analyst.reporting.telegram_fmt import format_alert, format_digest
from analyst.storage.analyses import save_analysis
from analyst.storage.db import init_db
from analyst.tracking.outcomes import OutcomeTracker

log = logging.getLogger(__name__)


def build_service(offline: bool = False, settings: Settings | None = None) -> AnalystService:
    """Construct the full service.

    `offline=True` swaps every network provider for the deterministic synthetic
    ones, so the system is fully runnable and demonstrable with no keys, no
    internet, and no waiting.
    """
    settings = settings or load_settings()
    init_db()

    if offline:
        price_providers = [SyntheticProvider()]
        fundamentals = SyntheticFundamentals()
        macro_loader = None
        cot_loader = None
    else:
        fallbacks = {e.symbol: e.fallback_symbols for e in load_watchlist() if e.fallback_symbols}
        price_providers = [YahooProvider(fallbacks=fallbacks), StooqProvider()]
        fundamentals = YahooFundamentals()
        macro_loader = MacroLoader()
        cot_provider = CotProvider()
        cot_loader = cot_provider.fetch

    repository = CandleRepository(price_providers)
    context_builder = ContextBuilder(
        repository=repository,
        settings=settings,
        fundamentals=fundamentals,
        calendar=EconomicCalendar(),
        macro_loader=macro_loader,
        cot_loader=cot_loader,
    )
    pipeline = Pipeline(context_builder, settings, load_gates())
    return AnalystService(
        settings=settings,
        repository=repository,
        pipeline=pipeline,
        notifier=TelegramNotifier(Secrets.from_env()),
        offline=offline,
    )


class AnalystService:
    def __init__(
        self,
        settings: Settings,
        repository: CandleRepository,
        pipeline: Pipeline,
        notifier: TelegramNotifier,
        offline: bool = False,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.pipeline = pipeline
        self.notifier = notifier
        self.offline = offline
        self.tracker = OutcomeTracker(repository, Timeframe.H1)

    # ------------------------------------------------------------------ #

    def analyse_one(
        self, instrument: Instrument, as_of: datetime | None = None, refresh: bool = True
    ) -> AnalysisResult:
        return self.pipeline.analyse(instrument, as_of=as_of, refresh=refresh)

    def run_once(
        self,
        as_of: datetime | None = None,
        persist: bool = True,
        alert: bool = True,
        symbols: list[str] | None = None,
    ) -> list[AnalysisResult]:
        """Analyse the whole watchlist. One failing symbol never stops the rest."""
        instruments = active_instruments()
        if symbols:
            wanted = {s.upper() for s in symbols}
            instruments = [i for i in instruments if i.symbol.upper() in wanted]

        results: list[AnalysisResult] = []
        for instrument in instruments:
            try:
                result = self.analyse_one(instrument, as_of=as_of)
            except Exception as exc:  # noqa: BLE001 - per-symbol isolation
                log.error("Analysis failed for %s: %s", instrument.symbol, exc)
                continue

            results.append(result)
            if persist:
                save_analysis(result)
            if alert:
                self._maybe_alert(result)

        if persist and results:
            try:
                counts = self.tracker.update_open_signals()
                if counts:
                    log.info("Open signal outcomes updated: %s", counts)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not update signal outcomes: %s", exc)

        return results

    def send_digest(self, results: list[AnalysisResult]) -> bool:
        if not self.notifier.enabled or not results:
            return False
        ok, detail = self.notifier.send(format_digest(results, self.settings.timezone.display))
        if not ok:
            log.warning("Could not send the daily digest: %s", detail)
        return ok

    def prune(self) -> int:
        return self.repository.prune(self.settings.storage.candle_retention_days)

    # ------------------------------------------------------------------ #

    def _maybe_alert(self, result: AnalysisResult) -> None:
        send, reason = should_alert(result, self.settings.alerts)
        if not send:
            log.debug("No alert for %s: %s", result.symbol, reason)
            return
        if not self.notifier.enabled:
            log.info(
                "Qualified signal on %s (%s) but Telegram is not configured",
                result.symbol, result.grade.value,
            )
            return
        text = format_alert(result, self.settings.timezone.display)
        if self.notifier.send_alert(result, text):
            log.info("Alert sent for %s (%s): %s", result.symbol, result.grade.value, reason)
