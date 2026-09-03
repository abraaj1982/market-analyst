"""Weighted scoring — the mathematical core of the system.

This is the component that must never be an AI. Everything upstream produces
numbers; this turns them into one number by an explicit, auditable formula that
anyone can recompute by hand from the stored breakdown.

    (1) Effective weight per engine
        w_eff(i) = w_base(i) x regime_mult(i) x asset_mult(i) x quality(i)

        Quality multiplies the weight rather than the score: an engine that does
        not trust its own inputs loses *influence*, it does not get pushed
        toward neutral. Those are different things, and conflating them is how
        a half-blind engine ends up diluting a confident one.

    (2) Signed consensus                S = sum(w_eff(i) x signed(i)) / sum(w_eff(i))
        where signed(i) = direction(i) x strength(i), in [-1, 1].

    (3) Dispersion (disagreement)       D = sum(w_eff(i) x (signed(i) - S)^2) / sum(w_eff(i))
        D is already normalised to [0, 1]: it reaches 1 only when weight is split
        evenly between +1 and -1.

    (4) Coherence                       C = 1 - lambda x D

    (5) Calibration                     K = L(|S|)
        A weighted mean is structurally compressed toward zero: even a textbook
        setup rarely exceeds |S| = 0.75, because most sub-signals are quiet most
        of the time. Reading |S| directly as a percentage would put A+ out of
        reach and make the whole grade scale decorative.

        So |S| passes through a normalised logistic (Platt scaling):

            L(x) = (sig(k(x - x0)) - sig(-k*x0)) / (sig(k(1 - x0)) - sig(-k*x0))

        which is strictly monotonic with L(0) = 0 and L(1) = 1. It changes the
        *scale*, never the ordering: a stronger consensus always calibrates to a
        higher confidence. `midpoint` and `steepness` are explicit priors in
        settings.yaml and are meant to be re-fitted from live outcome data.

    (6) Confidence
        confidence = K x C x news_factor x data_quality x regime_fit

        Every term after K is a multiplier in (0, 1]. Nothing here can raise
        confidence above the calibrated consensus - the system can only ever
        talk itself *down*. That asymmetry is deliberate.

    (7) Grade from thresholds, with a floor: if too little effective weight took
        part, the answer is NO_TRADE regardless of how clean the survivors look.
"""
from __future__ import annotations

import math

from analyst.core.config import CalibrationSettings, Settings
from analyst.core.enums import AssetClass, Direction, EngineId, Grade, Regime
from analyst.core.models import EngineContribution, EngineResult, ScoreBreakdown


def calibrate(raw: float, cfg: CalibrationSettings) -> float:
    """Normalised logistic on [0, 1]. Strictly increasing; L(0)=0, L(1)=1."""
    x = max(0.0, min(1.0, raw))
    k, x0 = cfg.steepness, cfg.midpoint
    sig = lambda z: 1.0 / (1.0 + math.exp(-z))  # noqa: E731
    lo, hi = sig(-k * x0), sig(k * (1.0 - x0))
    if hi <= lo:
        return x
    return (sig(k * (x - x0)) - lo) / (hi - lo)


class Aggregator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def effective_weight(
        self, engine: EngineId, quality: float, regime: Regime, asset_class: AssetClass
    ) -> tuple[float, float]:
        """Return (base weight after multipliers, effective weight incl. quality)."""
        weights = self.settings.weights
        base = float(weights.base.get(engine.value, 0.0))

        regime_mult = weights.regime_multipliers.get(regime.value, {}).get(engine.value, 1.0)
        asset_mult = weights.asset_class_multipliers.get(asset_class.value, {}).get(
            engine.value, 1.0
        )
        adjusted = base * float(regime_mult) * float(asset_mult)
        return adjusted, adjusted * max(0.0, min(1.0, quality))

    def aggregate(
        self,
        results: list[EngineResult],
        regime: Regime,
        asset_class: AssetClass,
        data_quality: float,
        news_factor: float = 1.0,
    ) -> ScoreBreakdown:
        cfg = self.settings.scoring
        contributions: list[EngineContribution] = []

        weighted_sum = 0.0
        weight_total = 0.0
        available_weight = 0.0
        signed_pairs: list[tuple[float, float]] = []  # (effective weight, signed score)

        for result in results:
            adjusted, effective = self.effective_weight(
                result.engine, result.quality, regime, asset_class
            )
            # `adjusted` ignores quality, so it is what this engine *would* have
            # been worth had its data been perfect - the denominator of coverage.
            available_weight += adjusted
            signed = result.signed
            if result.active and effective > 0.0:
                weighted_sum += effective * signed
                weight_total += effective
                signed_pairs.append((effective, signed))
            else:
                effective = 0.0

            contributions.append(
                EngineContribution(
                    engine=result.engine,
                    weight=round(adjusted, 4),
                    direction=result.direction,
                    strength=round(result.strength, 4),
                    quality=round(result.quality, 4),
                    effective_weight=round(effective, 4),
                    contribution=round(effective * signed, 4),
                    skipped_reason=result.skipped_reason,
                )
            )

        active = sum(1 for c in contributions if c.effective_weight > 0)

        if weight_total <= 0.0:
            return ScoreBreakdown(
                raw_signed_score=0.0, coherence=0.0, dispersion=0.0,
                data_quality=data_quality, news_factor=news_factor, regime_fit=0.0,
                confidence=0.0, contributions=contributions, total_effective_weight=0.0,
                available_weight=round(available_weight, 4), active_engines=0,
            )

        consensus = weighted_sum / weight_total
        dispersion = sum(w * (s - consensus) ** 2 for w, s in signed_pairs) / weight_total
        coherence = max(0.0, 1.0 - cfg.dispersion_lambda * dispersion)
        regime_fit = float(cfg.regime_fit.get(regime.value, 1.0))

        calibrated = calibrate(abs(consensus), cfg.calibration)
        confidence = calibrated * coherence * news_factor * data_quality * regime_fit

        return ScoreBreakdown(
            raw_signed_score=round(consensus, 4),
            calibrated_consensus=round(calibrated, 4),
            coherence=round(coherence, 4),
            dispersion=round(dispersion, 4),
            data_quality=round(data_quality, 4),
            news_factor=round(news_factor, 4),
            regime_fit=round(regime_fit, 4),
            confidence=round(max(0.0, min(1.0, confidence)), 4),
            contributions=contributions,
            total_effective_weight=round(weight_total, 4),
            available_weight=round(available_weight, 4),
            active_engines=active,
        )

    def direction(self, breakdown: ScoreBreakdown) -> Direction:
        return Direction.from_score(
            breakdown.raw_signed_score, self.settings.scoring.direction_deadband
        )

    def grade(self, breakdown: ScoreBreakdown) -> Grade:
        """Map confidence onto a grade, with a coverage floor.

        The floor matters more than the thresholds: one engine agreeing with
        itself while eight stood aside is not an A+ setup, it is a thin sample
        wearing an A+ costume. Coverage is measured as a *ratio* of the weight
        that was available for this asset class and regime, because the absolute
        weight differs enormously between, say, a metal and an equity.
        """
        cfg = self.settings.scoring
        if breakdown.active_engines < cfg.min_active_engines:
            return Grade.NO_TRADE
        if breakdown.coverage_ratio < cfg.min_coverage_ratio:
            return Grade.NO_TRADE

        c = breakdown.confidence
        thresholds = cfg.grades
        if c >= thresholds.A_PLUS:
            return Grade.A_PLUS
        if c >= thresholds.A:
            return Grade.A
        if c >= thresholds.B:
            return Grade.B
        if c >= thresholds.C:
            return Grade.C
        return Grade.NO_TRADE

    def coverage_shortfall(self, breakdown: ScoreBreakdown) -> str | None:
        """Human-readable reason when coverage blocked a grade, else None."""
        cfg = self.settings.scoring
        if breakdown.active_engines < cfg.min_active_engines:
            return (
                f"Only {breakdown.active_engines} active engines, below the "
                f"minimum of {cfg.min_active_engines}"
            )
        if breakdown.coverage_ratio < cfg.min_coverage_ratio:
            return (
                f"Coverage {breakdown.coverage_ratio:.0%} of available weight, below "
                f"the {cfg.min_coverage_ratio:.0%} floor"
            )
        return None
