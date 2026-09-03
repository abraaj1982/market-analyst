"""Hard gates, risk geometry, alert suppression, outcome tracking and stats."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from analyst.core.clock import UTC
from analyst.core.config import GateSpec, load_gates
from analyst.core.enums import Direction, GateStatus, Grade
from analyst.core.models import EngineResult, ScoreBreakdown
from analyst.data.providers.calendar import EconomicCalendar
from analyst.scoring.gates import GateEvaluator
from analyst.scoring.risk import build_plan
from analyst.storage.models import Signal
from analyst.tracking.outcomes import evaluate
from analyst.tracking.stats import wilson_interval
from tests.conftest import make_frame


def breakdown(confidence=0.8, engines=6, available=5.0, effective=4.5) -> ScoreBreakdown:
    return ScoreBreakdown(
        raw_signed_score=0.5, calibrated_consensus=0.8, coherence=0.95, dispersion=0.05,
        data_quality=1.0, news_factor=1.0, regime_fit=1.0, confidence=confidence,
        total_effective_weight=effective, available_weight=available, active_engines=engines,
    )


@pytest.fixture
def evaluator(settings):
    return GateEvaluator(settings, load_gates())


# ------------------------------------------------------------------- gates


def test_not_evaluated_is_never_a_pass(settings, gold_context):
    """The single most important gate property: an unmeasurable condition is not
    a satisfied one."""
    spec = GateSpec(id="news_blackout", label_ar="أخبار", blocking=True, params={})
    evaluator = GateEvaluator(settings, [spec])
    gold_context.extras.pop("calendar_events", None)
    results = evaluator.evaluate(gold_context, Direction.BULLISH, breakdown(), {}, None)
    assert results[0].status is GateStatus.NOT_EVALUATED
    assert results[0].status is not GateStatus.PASSED


def test_confidence_gate_blocks_low_scores(settings, gold_context):
    spec = GateSpec(id="min_confidence", label_ar="ثقة", params={"min_confidence": 0.6})
    evaluator = GateEvaluator(settings, [spec])
    low = evaluator.evaluate(gold_context, Direction.BULLISH, breakdown(0.4), {}, None)[0]
    high = evaluator.evaluate(gold_context, Direction.BULLISH, breakdown(0.75), {}, None)[0]
    assert low.status is GateStatus.FAILED
    assert high.status is GateStatus.PASSED


def test_mtf_alignment_gate(settings, gold_context):
    spec = GateSpec(id="mtf_alignment", label_ar="توافق", params={"timeframes": ["1d", "4h"]})
    evaluator = GateEvaluator(settings, [spec])
    aligned = EngineResult(engine="trend", direction=Direction.BULLISH, strength=0.8, quality=1.0,
                           metrics={"1d_score": 0.6, "4h_score": 0.5})
    split = EngineResult(engine="trend", direction=Direction.BULLISH, strength=0.4, quality=1.0,
                         metrics={"1d_score": -0.6, "4h_score": 0.5})
    assert evaluator.evaluate(gold_context, Direction.BULLISH, breakdown(), {"trend": aligned}, None)[0].status is GateStatus.PASSED
    assert evaluator.evaluate(gold_context, Direction.BULLISH, breakdown(), {"trend": split}, None)[0].status is GateStatus.FAILED


def test_shortable_gate_blocks_shorts_in_long_only_markets(settings, gold_context):
    spec = GateSpec(id="shortable", label_ar="قابلية التنفيذ", params={})
    evaluator = GateEvaluator(settings, [spec])

    assert evaluator.evaluate(gold_context, Direction.BEARISH, breakdown(), {}, None)[0].status is GateStatus.PASSED

    object.__setattr__(gold_context.instrument, "shortable", False)
    blocked = evaluator.evaluate(gold_context, Direction.BEARISH, breakdown(), {}, None)[0]
    long_ok = evaluator.evaluate(gold_context, Direction.BULLISH, breakdown(), {}, None)[0]
    assert blocked.status is GateStatus.FAILED
    assert long_ok.status is GateStatus.PASSED


def test_engine_coverage_gate(settings, gold_context):
    spec = GateSpec(id="engine_coverage", label_ar="تغطية",
                    params={"min_active_engines": 3, "min_effective_weight": 0.5})
    evaluator = GateEvaluator(settings, [spec])
    thin = breakdown(engines=1, effective=0.6, available=5.0)
    full = breakdown(engines=6, effective=4.5, available=5.0)
    assert evaluator.evaluate(gold_context, Direction.BULLISH, thin, {}, None)[0].status is GateStatus.FAILED
    assert evaluator.evaluate(gold_context, Direction.BULLISH, full, {}, None)[0].status is GateStatus.PASSED


# ---------------------------------------------------------------- calendar


def test_calendar_generates_nfp_on_first_friday():
    calendar = EconomicCalendar()
    start = datetime(2026, 9, 1, tzinfo=UTC)
    events = calendar.events_between(start, start + timedelta(days=10))
    nfp = [e for e in events if e.id == "NFP"]
    assert len(nfp) == 1
    assert nfp[0].when.weekday() == 4          # Friday
    assert nfp[0].when.day <= 7                # the first one of the month


def test_calendar_blackout_window():
    calendar = EconomicCalendar()
    release = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
    assert calendar.in_blackout(release - timedelta(minutes=30)) is not None
    assert calendar.in_blackout(release + timedelta(minutes=20)) is not None
    assert calendar.in_blackout(release - timedelta(hours=4)) is None


# -------------------------------------------------------------------- risk


def test_risk_plan_geometry(settings, gold_context):
    plan = build_plan(gold_context, Direction.BULLISH, settings.risk)
    assert plan is not None
    assert plan.stop_loss < plan.entry < plan.take_profit_1 < plan.take_profit_2
    assert plan.risk_reward == pytest.approx(settings.risk.target_r_multiple_1)


def test_risk_stop_respects_floor_and_ceiling(settings, gold_context):
    for direction in (Direction.BULLISH, Direction.BEARISH):
        plan = build_plan(gold_context, direction, settings.risk)
        assert plan is not None
        multiple = plan.stop_distance / plan.atr
        assert settings.risk.atr_stop_multiplier - 1e-6 <= multiple <= settings.risk.max_stop_atr + 1e-6


def test_no_plan_for_neutral(settings, gold_context):
    assert build_plan(gold_context, Direction.NEUTRAL, settings.risk) is None


# ------------------------------------------------------------------ dedupe


def test_dedupe_blocks_below_min_grade(settings, pipeline, gold):
    from analyst.alerts.dedupe import should_alert

    result = pipeline.analyse(gold)
    result.grade = Grade.C
    send, reason = should_alert(result, settings.alerts)
    assert send is False
    assert "دون الحد الأدنى" in reason


def test_dedupe_blocks_when_a_gate_failed(settings, pipeline, gold):
    from analyst.alerts.dedupe import should_alert
    from analyst.core.models import GateResult

    result = pipeline.analyse(gold)
    result.grade = Grade.A_PLUS
    result.gates = [GateResult(gate="x", label_ar="بوابة", status=GateStatus.FAILED, blocking=True)]
    send, reason = should_alert(result, settings.alerts)
    assert send is False
    assert "بوابة صلبة" in reason


# ----------------------------------------------------------------- outcomes


def signal(direction: int = 1, entry: float = 100.0) -> Signal:
    return Signal(
        analysis_id=1, symbol="TEST", issued_at=datetime(2024, 1, 1, tzinfo=UTC),
        direction=direction, grade="A", confidence=0.8,
        entry=entry, stop_loss=entry - 2 * direction,
        take_profit_1=entry + 4 * direction, take_profit_2=entry + 7 * direction,
        risk_reward=2.0, status="open",
    )


def test_outcome_target_hit():
    candles = make_frame([100, 101, 102, 103, 105], start="2024-01-01 01:00")
    result = evaluate(signal(), candles)
    assert result.status == "tp1_hit"
    assert result.r_multiple == pytest.approx(2.0)


def test_outcome_stop_hit():
    candles = make_frame([100, 99, 98, 97], start="2024-01-01 01:00")
    result = evaluate(signal(), candles)
    assert result.status == "stopped"
    assert result.r_multiple == pytest.approx(-1.0)


def test_outcome_prefers_stop_when_one_bar_covers_both():
    """Without intrabar data the order is unknowable, so the pessimistic reading
    is the only honest one. Assuming otherwise is how backtests invent profits."""
    candles = make_frame([100], start="2024-01-01 01:00")
    candles.loc[candles.index[0], ["high", "low"]] = [110.0, 90.0]
    result = evaluate(signal(), candles)
    assert result.status == "stopped"


def test_outcome_expires():
    closes = [100.5] * 40
    candles = make_frame(closes, start="2024-01-01 01:00", freq="D")
    result = evaluate(signal(), candles, expiry=timedelta(days=21))
    assert result.status == "expired"


def test_outcome_short_direction():
    candles = make_frame([100, 99, 98, 97, 95], start="2024-01-01 01:00")
    result = evaluate(signal(direction=-1), candles)
    assert result.status == "tp1_hit"


# -------------------------------------------------------------------- stats


def test_wilson_interval_widens_on_small_samples():
    narrow = wilson_interval(70, 100)
    wide = wilson_interval(7, 10)
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])
    assert 0.0 <= wide[0] <= wide[1] <= 1.0


def test_stats_refuse_to_report_thin_samples():
    from analyst.tracking.stats import compute

    result = compute()
    assert result.sample == 0
    assert result.win_rate is None
    assert "الحد الأدنى" in result.headline_ar
