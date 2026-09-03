"""Hard gates: boolean requirements evaluated independently of the score.

A gate is not a weight. A 95% confidence reading with a high-impact release in
40 minutes is not a 95% opportunity — it is not an opportunity at all. Encoding
that as a small negative weight would let a strong enough consensus buy its way
past it, which is precisely what must not happen.

Every gate returns one of three states, and `NOT_EVALUATED` is never treated as
a pass. If the calendar could not be loaded, the news gate did not clear — it
simply did not run, and the report says so.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np

from analyst.core.config import GateSpec, Settings
from analyst.core.enums import Direction, GateStatus, Timeframe
from analyst.core.models import (
    EngineResult,
    GateResult,
    MarketContext,
    RiskPlan,
    ScoreBreakdown,
)


class GateEvaluator:
    def __init__(self, settings: Settings, specs: list[GateSpec]) -> None:
        self.settings = settings
        self.specs = specs

    def evaluate(
        self,
        ctx: MarketContext,
        direction: Direction,
        breakdown: ScoreBreakdown,
        engines: dict[str, EngineResult],
        risk: RiskPlan | None,
    ) -> list[GateResult]:
        handlers = {
            "data_quality": self._data_quality,
            "mtf_alignment": self._mtf_alignment,
            "min_confidence": self._min_confidence,
            "risk_reward": self._risk_reward,
            "news_blackout": self._news_blackout,
            "engine_coverage": self._engine_coverage,
            "not_against_higher_trend": self._not_against_higher_trend,
            "shortable": self._shortable,
            "earnings_blackout": self._earnings_blackout,
            "volatility_sane": self._volatility_sane,
        }
        out: list[GateResult] = []
        for spec in self.specs:
            handler = handlers.get(spec.id)
            if handler is None:
                out.append(
                    GateResult(gate=spec.id, label=spec.label,
                               status=GateStatus.NOT_EVALUATED,
                               detail="No implementation for this gate",
                               blocking=spec.blocking)
                )
                continue
            status, detail = handler(spec, ctx, direction, breakdown, engines, risk)
            out.append(
                GateResult(gate=spec.id, label=spec.label, status=status,
                           detail=detail, blocking=spec.blocking)
            )
        return out

    # ------------------------------------------------------------------ #
    # Individual gates. Signature is uniform so the dispatch table stays flat.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _data_quality(spec, ctx, direction, breakdown, engines, risk):
        minimum = float(spec.params.get("min_score", 0.7))
        score = ctx.quality.score
        ok = score >= minimum
        return (
            GateStatus.PASSED if ok else GateStatus.FAILED,
            f"Data quality {score:.0%} against a {minimum:.0%} floor",
        )

    def _mtf_alignment(self, spec, ctx, direction, breakdown, engines, risk):
        required = [Timeframe(t) for t in spec.params.get("timeframes", ["1d", "4h"])]
        trend = engines.get("trend")
        if trend is None or trend.skipped_reason:
            return GateStatus.NOT_EVALUATED, "The trend engine did not run"

        scores = {}
        for tf in required:
            key = f"{tf.value}_score"
            if key not in trend.metrics:
                return GateStatus.NOT_EVALUATED, f"No reading for the {tf.label} frame"
            scores[tf] = trend.metrics[key]

        deadband = self.settings.scoring.direction_deadband
        signs = {tf: np.sign(v) if abs(v) >= deadband else 0 for tf, v in scores.items()}
        target = float(direction.value)
        ok = all(s == target for s in signs.values())
        detail = " · ".join(f"{tf.label}: {scores[tf]:+.2f}" for tf in required)
        return (GateStatus.PASSED if ok else GateStatus.FAILED), detail

    def _min_confidence(self, spec, ctx, direction, breakdown, engines, risk):
        minimum = float(spec.params.get("min_confidence", 0.6))
        ok = breakdown.confidence >= minimum
        return (
            GateStatus.PASSED if ok else GateStatus.FAILED,
            f"Confidence {breakdown.confidence:.0%} against a {minimum:.0%} floor",
        )

    @staticmethod
    def _risk_reward(spec, ctx, direction, breakdown, engines, risk):
        minimum = float(spec.params.get("min_rr", 1.8))
        if risk is None:
            return GateStatus.NOT_EVALUATED, "No risk plan (no clear direction)"
        ok = risk.risk_reward >= minimum
        return (
            GateStatus.PASSED if ok else GateStatus.FAILED,
            f"Reward-to-risk {risk.risk_reward:.2f} against a {minimum:.2f} floor",
        )

    @staticmethod
    def _news_blackout(spec, ctx, direction, breakdown, engines, risk):
        if ctx.extras.get("calendar_events") is None:
            return GateStatus.NOT_EVALUATED, "Economic calendar not loaded"
        blackout = ctx.extras.get("calendar_blackout")
        if blackout is not None:
            return (
                GateStatus.FAILED,
                f"{blackout.label} at {blackout.when:%H:%M} UTC — inside the blackout window",
            )
        upcoming = ctx.extras.get("next_high_impact")
        if upcoming is not None:
            hours = (upcoming.when - ctx.as_of).total_seconds() / 3600.0
            return GateStatus.PASSED, f"Next high-impact release in {hours:.1f}h ({upcoming.label})"
        return GateStatus.PASSED, "No high-impact release on the near horizon"

    def _engine_coverage(self, spec, ctx, direction, breakdown, engines, risk):
        min_engines = int(spec.params.get("min_active_engines", 3))
        min_ratio = float(
            spec.params.get("min_effective_weight", self.settings.scoring.min_coverage_ratio)
        )
        ok = breakdown.active_engines >= min_engines and breakdown.coverage_ratio >= min_ratio
        return (
            GateStatus.PASSED if ok else GateStatus.FAILED,
            f"{breakdown.active_engines} active engines · {breakdown.coverage_ratio:.0%} coverage "
            f"(floor: {min_engines} engines and {min_ratio:.0%})",
        )

    def _not_against_higher_trend(self, spec, ctx, direction, breakdown, engines, risk):
        tf = Timeframe(spec.params.get("timeframe", "1d"))
        trend = engines.get("trend")
        if trend is None or f"{tf.value}_score" not in trend.metrics:
            return GateStatus.NOT_EVALUATED, f"No trend reading on {tf.label}"
        score = trend.metrics[f"{tf.value}_score"]
        deadband = self.settings.scoring.direction_deadband
        if abs(score) < deadband:
            return GateStatus.PASSED, f"{tf.label} trend is neutral ({score:+.2f}) — no conflict"
        against = np.sign(score) != float(direction.value)
        return (
            GateStatus.FAILED if against else GateStatus.PASSED,
            f"{tf.label} trend {score:+.2f} against a {direction.label} signal",
        )

    @staticmethod
    def _shortable(spec, ctx, direction, breakdown, engines, risk):
        if direction is not Direction.BEARISH:
            return GateStatus.PASSED, "Bullish signal — no execution constraint"
        if ctx.instrument.shortable:
            return GateStatus.PASSED, "Short selling is available in this market"
        return (
            GateStatus.FAILED,
            "Retail short selling is not available in this market — a bearish "
            "reading means exit or avoid, not a short trade",
        )

    @staticmethod
    def _earnings_blackout(spec, ctx, direction, breakdown, engines, risk):
        if not ctx.instrument.is_equity:
            return GateStatus.PASSED, "Not applicable outside equities"
        fundamentals = ctx.extras.get("fundamentals") or {}
        next_earnings = fundamentals.get("next_earnings")
        if next_earnings is None:
            return GateStatus.NOT_EVALUATED, "Next earnings date unknown"
        before = timedelta(days=int(spec.params.get("days_before", 2)))
        after = timedelta(days=int(spec.params.get("days_after", 1)))
        delta = next_earnings.to_pydatetime() - ctx.as_of
        if -after <= delta <= before:
            return (
                GateStatus.FAILED,
                f"Earnings on {next_earnings:%Y-%m-%d} — inside the blackout window",
            )
        return GateStatus.PASSED, f"Next earnings on {next_earnings:%Y-%m-%d}"

    @staticmethod
    def _volatility_sane(spec, ctx, direction, breakdown, engines, risk):
        rank = ctx.extras.get("regime_metrics", {}).get("atr_percentile")
        if rank is None:
            return GateStatus.NOT_EVALUATED, "Volatility percentile not computed"
        lo = float(spec.params.get("min_atr_percentile", 0.10))
        hi = float(spec.params.get("max_atr_percentile", 0.95))
        ok = lo <= rank <= hi
        return (
            GateStatus.PASSED if ok else GateStatus.FAILED,
            f"Volatility at the {rank:.0%} percentile (accepted band {lo:.0%}–{hi:.0%})",
        )
