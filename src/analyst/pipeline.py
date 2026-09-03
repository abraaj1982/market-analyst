"""Analysis pipeline — wires data, engines, scoring, gating and reporting.

One public entry point, `Pipeline.analyse(instrument)`, which is deliberately
side-effect-light: it builds a context, runs the engines, aggregates, gates,
plans risk and renders a report. Persisting and alerting are separate steps so
that a dry run, a backtest and a live run can share exactly the same code path.
"""
from __future__ import annotations

import logging
from datetime import datetime

from analyst.core.clock import now_utc
from analyst.core.config import GateSpec, Settings
from analyst.core.enums import Direction, EngineId, Grade
from analyst.core.models import AnalysisResult, EngineResult, Instrument, MarketContext
from analyst.data.context import ContextBuilder
from analyst.engines.base import Engine
from analyst.engines.classic_ta import ClassicTaEngine
from analyst.engines.cot import CotEngine
from analyst.engines.fundamentals import FundamentalsEngine
from analyst.engines.ict_smc import IctSmcEngine
from analyst.engines.indicators import IndicatorEngine
from analyst.engines.macro import MacroEngine
from analyst.engines.news import NewsEngine
from analyst.engines.trend import TrendEngine
from analyst.engines.volume_seasonality import VolumeSeasonalityEngine
from analyst.reporting.narrative import build_report
from analyst.scoring.aggregator import Aggregator
from analyst.scoring.gates import GateEvaluator
from analyst.scoring.risk import build_plan
from analyst.version import code_version

log = logging.getLogger(__name__)


def default_engines(settings: Settings) -> list[Engine]:
    """Engine order is presentation only — the aggregator is order-independent."""
    return [
        TrendEngine(settings),
        IctSmcEngine(settings),
        ClassicTaEngine(settings),
        IndicatorEngine(settings),
        MacroEngine(settings),
        CotEngine(settings),
        VolumeSeasonalityEngine(settings),
        FundamentalsEngine(settings),
        NewsEngine(settings),
    ]


class Pipeline:
    def __init__(
        self,
        context_builder: ContextBuilder,
        settings: Settings,
        gate_specs: list[GateSpec],
        engines: list[Engine] | None = None,
    ) -> None:
        self.context_builder = context_builder
        self.settings = settings
        self.engines = engines if engines is not None else default_engines(settings)
        self.aggregator = Aggregator(settings)
        self.gate_evaluator = GateEvaluator(settings, gate_specs)

    def analyse(
        self, instrument: Instrument, as_of: datetime | None = None, refresh: bool = True
    ) -> AnalysisResult:
        as_of = as_of or now_utc()
        ctx = self.context_builder.build(instrument, as_of=as_of, refresh=refresh)
        return self.analyse_context(ctx)

    def analyse_context(self, ctx: MarketContext) -> AnalysisResult:
        """Run everything against an already-built context.

        Split out from `analyse` so backtests can replay stored contexts without
        touching the network, and so tests can inject a hand-built context.
        """
        results: list[EngineResult] = [engine.analyse(ctx) for engine in self.engines]
        by_id = {r.engine.value: r for r in results}

        news_factor = self._news_factor(by_id.get(EngineId.NEWS.value))

        # The news engine is a modifier and a gate, never a voter.
        voters = [r for r in results if r.engine is not EngineId.NEWS]

        breakdown = self.aggregator.aggregate(
            voters,
            regime=ctx.regime,
            asset_class=ctx.instrument.asset_class,
            data_quality=ctx.quality.score,
            news_factor=news_factor,
        )
        direction = self.aggregator.direction(breakdown)
        grade = self.aggregator.grade(breakdown)

        risk = build_plan(ctx, direction, self.settings.risk) if direction is not Direction.NEUTRAL else None
        gates = self.gate_evaluator.evaluate(ctx, direction, breakdown, by_id, risk)

        result = AnalysisResult(
            symbol=ctx.instrument.symbol,
            name_ar=ctx.instrument.name_ar,
            market=ctx.instrument.market,
            as_of=ctx.as_of,
            spot=ctx.spot,
            direction=direction,
            confidence=breakdown.confidence,
            grade=grade,
            regime=ctx.regime,
            breakdown=breakdown,
            engines=results,
            gates=gates,
            risk=risk,
            data_quality_issues=ctx.quality.issues,
            config_version=self.settings.version,
            code_version=code_version(),
        )

        # A blocked setup is downgraded so that nothing downstream — alerts,
        # digest, dashboard — has to remember to re-check the gates.
        if result.blocking_failures and grade in (Grade.A_PLUS, Grade.A):
            result.grade = Grade.B

        result.report_ar = build_report(result, self.settings.timezone.display)
        return result

    @staticmethod
    def _news_factor(news: EngineResult | None) -> float:
        if news is None or news.skipped_reason:
            return 1.0
        return float(news.metrics.get("news_factor", 1.0))
