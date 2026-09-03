"""The confidence maths: consensus, dispersion, calibration, coverage floors."""
from __future__ import annotations

import pytest

from analyst.core.enums import AssetClass, Direction, EngineId, Grade, Regime
from analyst.core.models import EngineResult
from analyst.scoring.aggregator import Aggregator, calibrate

ALL_ENGINES = [
    EngineId.TREND, EngineId.ICT_SMC, EngineId.CLASSIC_TA, EngineId.INDICATORS,
    EngineId.MACRO, EngineId.COT, EngineId.VOLUME_SEASONALITY, EngineId.FUNDAMENTALS,
]


def result(engine, direction, strength, quality=1.0):
    return EngineResult(engine=engine, direction=direction, strength=strength, quality=quality)


def unanimous(direction=Direction.BULLISH, strength=0.85, engines=None):
    return [result(e, direction, strength) for e in (engines or ALL_ENGINES)]


@pytest.fixture
def aggregator(settings):
    return Aggregator(settings)


# ------------------------------------------------------------------ calibration


def test_calibration_is_monotonic_and_anchored(settings):
    cfg = settings.scoring.calibration
    values = [calibrate(x / 100, cfg) for x in range(101)]
    assert values[0] == pytest.approx(0.0)
    assert values[-1] == pytest.approx(1.0)
    assert all(b >= a for a, b in zip(values, values[1:], strict=False))


def test_calibration_separates_trend_from_noise(settings):
    """Measured empirically: a clean multi-timeframe trend produces |S| ~ 0.46,
    a random walk ~ 0.17. The curve must keep those far apart."""
    cfg = settings.scoring.calibration
    assert calibrate(0.46, cfg) > 0.70
    assert calibrate(0.17, cfg) < 0.20


# -------------------------------------------------------------------- consensus


def test_agreement_beats_conflict(aggregator):
    agree = aggregator.aggregate(unanimous(), Regime.TRENDING, AssetClass.METAL, 1.0)
    conflict = aggregator.aggregate(
        [
            result(EngineId.TREND, Direction.BULLISH, 0.85),
            result(EngineId.ICT_SMC, Direction.BEARISH, 0.85),
            result(EngineId.CLASSIC_TA, Direction.BULLISH, 0.85),
            result(EngineId.INDICATORS, Direction.BEARISH, 0.85),
            result(EngineId.MACRO, Direction.BULLISH, 0.85),
        ],
        Regime.TRENDING, AssetClass.METAL, 1.0,
    )
    assert agree.confidence > conflict.confidence
    assert conflict.dispersion > agree.dispersion
    assert conflict.coherence < agree.coherence


def test_dispersion_is_bounded(aggregator):
    breakdown = aggregator.aggregate(
        [
            result(EngineId.TREND, Direction.BULLISH, 1.0),
            result(EngineId.ICT_SMC, Direction.BEARISH, 1.0),
        ],
        Regime.TRENDING, AssetClass.METAL, 1.0,
    )
    assert 0.0 <= breakdown.dispersion <= 1.0
    assert 0.0 <= breakdown.coherence <= 1.0


# --------------------------------------------------------------------- modifiers


@pytest.mark.parametrize(
    "kwargs",
    [
        {"data_quality": 0.5},
        {"news_factor": 0.35},
    ],
)
def test_modifiers_only_reduce(aggregator, kwargs):
    base = aggregator.aggregate(unanimous(), Regime.TRENDING, AssetClass.METAL, 1.0, 1.0)
    reduced = aggregator.aggregate(
        unanimous(), Regime.TRENDING, AssetClass.METAL,
        kwargs.get("data_quality", 1.0), kwargs.get("news_factor", 1.0),
    )
    assert reduced.confidence < base.confidence


def test_regime_fit_never_exceeds_one(settings, aggregator):
    assert all(v <= 1.0 for v in settings.scoring.regime_fit.values())


# ------------------------------------------------------------------ asset rules


def test_fundamentals_are_zeroed_for_metals(aggregator):
    breakdown = aggregator.aggregate(
        [result(EngineId.FUNDAMENTALS, Direction.BULLISH, 1.0)],
        Regime.RANGING, AssetClass.METAL, 1.0,
    )
    assert breakdown.total_effective_weight == 0.0
    assert breakdown.confidence == 0.0


def test_cot_is_zeroed_for_equities(aggregator):
    breakdown = aggregator.aggregate(
        [result(EngineId.COT, Direction.BULLISH, 1.0)],
        Regime.RANGING, AssetClass.EQUITY, 1.0,
    )
    assert breakdown.total_effective_weight == 0.0


# ------------------------------------------------------------------- coverage


def test_single_engine_cannot_produce_a_grade(aggregator):
    """The failure mode this floor exists to prevent: one confident engine while
    the rest stood aside, dressed up as an A+ setup."""
    results = [result(EngineId.TREND, Direction.BULLISH, 0.95)] + [
        EngineResult.skipped(e, "لا بيانات") for e in ALL_ENGINES[1:]
    ]
    breakdown = aggregator.aggregate(results, Regime.RANGING, AssetClass.METAL, 1.0)
    assert breakdown.confidence > 0.8            # the raw number is high...
    assert aggregator.grade(breakdown) is Grade.NO_TRADE   # ...and still refused
    assert "المحركات الفعّالة" in aggregator.coverage_shortfall(breakdown)


def test_full_coverage_reaches_top_grade(aggregator):
    breakdown = aggregator.aggregate(
        unanimous(strength=0.95), Regime.TRENDING, AssetClass.METAL, 1.0
    )
    assert breakdown.coverage_ratio == pytest.approx(1.0)
    assert aggregator.grade(breakdown) in (Grade.A_PLUS, Grade.A)


def test_quality_reduces_weight_not_direction(aggregator):
    """A half-trusted engine loses influence; it does not get pushed to neutral."""
    confident = aggregator.aggregate(unanimous(), Regime.TRENDING, AssetClass.METAL, 1.0)
    unsure = aggregator.aggregate(
        [result(e, Direction.BULLISH, 0.85, quality=0.4) for e in ALL_ENGINES],
        Regime.TRENDING, AssetClass.METAL, 1.0,
    )
    assert unsure.total_effective_weight < confident.total_effective_weight
    # direction and consensus are unchanged: only the weight shrank
    assert unsure.raw_signed_score == pytest.approx(confident.raw_signed_score, abs=1e-9)
    assert unsure.coverage_ratio < 1.0


def test_empty_input_is_safe(aggregator):
    breakdown = aggregator.aggregate([], Regime.RANGING, AssetClass.FX, 1.0)
    assert breakdown.confidence == 0.0
    assert aggregator.grade(breakdown) is Grade.NO_TRADE


def test_neutral_engines_dilute_consensus(aggregator):
    """An engine with no view is not the same as an engine that is absent."""
    with_neutral = aggregator.aggregate(
        [
            result(EngineId.TREND, Direction.BULLISH, 0.9),
            result(EngineId.ICT_SMC, Direction.BULLISH, 0.9),
            result(EngineId.CLASSIC_TA, Direction.BULLISH, 0.9),
            result(EngineId.INDICATORS, Direction.NEUTRAL, 0.0),
        ],
        Regime.TRENDING, AssetClass.METAL, 1.0,
    )
    without = aggregator.aggregate(
        [
            result(EngineId.TREND, Direction.BULLISH, 0.9),
            result(EngineId.ICT_SMC, Direction.BULLISH, 0.9),
            result(EngineId.CLASSIC_TA, Direction.BULLISH, 0.9),
            EngineResult.skipped(EngineId.INDICATORS, "لا بيانات"),
        ],
        Regime.TRENDING, AssetClass.METAL, 1.0,
    )
    assert with_neutral.raw_signed_score < without.raw_signed_score
