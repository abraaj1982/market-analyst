"""Engine behaviour: correct verdicts on constructed data, and safe refusal."""
from __future__ import annotations

import numpy as np
import pytest

from analyst.core.enums import AssetClass, Direction, EngineId, Market, Regime, Timeframe
from analyst.core.models import (
    DataQualityReport,
    Instrument,
    MacroSnapshot,
    MarketContext,
    Series,
)
from analyst.engines.base import Engine, ScoreBuilder, scale
from analyst.engines.cot import CotEngine
from analyst.engines.fundamentals import FundamentalsEngine, _band
from analyst.engines.indicators import IndicatorEngine
from analyst.engines.macro import MacroEngine
from analyst.engines.trend import TrendEngine
from analyst.engines.volume_seasonality import VolumeSeasonalityEngine
from tests.conftest import make_frame


def build_context(
    instrument: Instrument,
    closes_by_tf: dict[Timeframe, np.ndarray],
    regime: Regime = Regime.TRENDING,
    quality: float = 1.0,
    extras: dict | None = None,
    macro: MacroSnapshot | None = None,
) -> MarketContext:
    series = {
        tf: Series(instrument, tf, make_frame(closes, freq="h" if tf.minutes < 1440 else "D"))
        for tf, closes in closes_by_tf.items()
    }
    from analyst.core.clock import now_utc

    return MarketContext(
        instrument=instrument,
        as_of=now_utc(),
        series=series,
        quality=DataQualityReport(score=quality),
        regime=regime,
        macro=macro or MacroSnapshot(),
        extras=extras or {},
    )


def trending_closes(n: int, slope: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100 + slope * np.arange(n) + rng.normal(0, 0.25, n)


# ---------------------------------------------------------------- ScoreBuilder


def test_score_builder_is_a_weighted_mean():
    builder = ScoreBuilder()
    builder.add("a", "أ", 1.0, 3.0)
    builder.add("b", "ب", -1.0, 1.0)
    assert builder.score == pytest.approx(0.5)


def test_score_builder_never_exceeds_bounds():
    builder = ScoreBuilder()
    for i in range(20):
        builder.add(f"e{i}", "دليل", 1.0, 1.0)
    assert builder.score == pytest.approx(1.0)


def test_scale_saturates():
    assert scale(10.0, 2.0) == 1.0
    assert scale(-10.0, 2.0) == -1.0
    assert scale(1.0, 2.0) == pytest.approx(0.5)


# ---------------------------------------------------------------- TrendEngine


def test_trend_engine_reads_direction(settings, gold):
    up = build_context(gold, {
        Timeframe.H1: trending_closes(600, 0.05),
        Timeframe.H4: trending_closes(400, 0.2),
        Timeframe.D1: trending_closes(400, 0.6),
    })
    down = build_context(gold, {
        Timeframe.H1: trending_closes(600, -0.05),
        Timeframe.H4: trending_closes(400, -0.2),
        Timeframe.D1: trending_closes(400, -0.6),
    })
    bullish = TrendEngine(settings).analyse(up)
    bearish = TrendEngine(settings).analyse(down)

    assert bullish.direction is Direction.BULLISH
    assert bearish.direction is Direction.BEARISH
    assert bullish.metrics["mtf_agreement"] == pytest.approx(1.0)


def test_trend_engine_detects_timeframe_conflict(settings, gold):
    ctx = build_context(gold, {
        Timeframe.H1: trending_closes(600, 0.06),
        Timeframe.H4: trending_closes(400, 0.25),
        Timeframe.D1: trending_closes(400, -0.6),
    })
    result = TrendEngine(settings).analyse(ctx)
    assert result.metrics["mtf_agreement"] < 1.0
    assert result.strength < 0.8


def test_trend_engine_skips_without_history(settings, gold):
    ctx = build_context(gold, {Timeframe.H1: trending_closes(80, 0.05)})
    result = TrendEngine(settings).analyse(ctx)
    assert result.skipped_reason is not None
    assert result.quality == 0.0


# ------------------------------------------------------------ IndicatorEngine


def test_indicator_engine_flips_reading_with_regime(settings, gold):
    closes = trending_closes(400, 0.15)
    trending = build_context(gold, {Timeframe.H1: closes, Timeframe.H4: closes,
                                    Timeframe.D1: closes}, regime=Regime.TRENDING)
    ranging = build_context(gold, {Timeframe.H1: closes, Timeframe.H4: closes,
                                   Timeframe.D1: closes}, regime=Regime.RANGING)

    momentum = IndicatorEngine(settings).analyse(trending)
    reversion = IndicatorEngine(settings).analyse(ranging)

    momentum_rsi = next(e for e in momentum.evidence if e.code == "rsi_momentum")
    reversion_rsi = next(e for e in reversion.evidence if e.code == "rsi_reversion")
    # same RSI, opposite sign: that is the whole point of regime-aware reading
    assert momentum_rsi.contribution > 0
    assert reversion_rsi.contribution < 0


# ----------------------------------------------------------------- MacroEngine


def test_macro_engine_reads_real_yields_for_gold(settings, gold):
    falling_real_yield = MacroSnapshot(
        values={"DFII10": 1.5, "DTWEXBGS": 120.0},
        changes={"DFII10": -0.30, "DTWEXBGS": -1.5},
        available=True,
    )
    ctx = build_context(gold, {Timeframe.D1: trending_closes(300, 0.1)},
                        macro=falling_real_yield)
    result = MacroEngine(settings).analyse(ctx)
    assert result.direction is Direction.BULLISH

    rising = MacroSnapshot(
        values={"DFII10": 2.1, "DTWEXBGS": 124.0},
        changes={"DFII10": +0.30, "DTWEXBGS": +1.5},
        available=True,
    )
    ctx2 = build_context(gold, {Timeframe.D1: trending_closes(300, 0.1)}, macro=rising)
    assert MacroEngine(settings).analyse(ctx2).direction is Direction.BEARISH


def test_macro_engine_declines_for_msx(settings):
    local = Instrument(
        symbol="BKMB", name="بنك مسقط", market=Market.MSX, asset_class=AssetClass.EQUITY,
        provider_symbol="BKMB", currency="OMR",
        supported_timeframes=(Timeframe.D1,), shortable=False,
    )
    ctx = build_context(local, {Timeframe.D1: trending_closes(300, 0.1)},
                        macro=MacroSnapshot(values={"DFII10": 2.0}, changes={"DFII10": 0.1},
                                            available=True))
    result = MacroEngine(settings).analyse(ctx)
    assert result.skipped_reason is not None
    assert "Omani" in result.skipped_reason


# ------------------------------------------------------------------ COT engine


def test_cot_engine_needs_history(settings, gold):
    ctx = build_context(gold, {Timeframe.D1: trending_closes(300, 0.1)}, extras={"cot": None})
    result = CotEngine(settings).analyse(ctx)
    assert result.skipped_reason is not None


# ------------------------------------------------------------ volume handling


def test_volume_engine_stands_aside_without_real_volume(settings, gold):
    """Spot FX reports no meaningful volume; scoring it would be scoring noise."""
    closes = trending_closes(400, 0.1)
    ctx = build_context(gold, {Timeframe.D1: closes, Timeframe.H1: closes})
    for series in ctx.series.values():
        series.df["volume"] = 0.0
    result = VolumeSeasonalityEngine(settings).analyse(ctx)
    assert any("volume" in note for note in result.notes) or result.skipped_reason


# ----------------------------------------------------------------- fundamentals


def test_fundamentals_band_handles_both_directions():
    # lower is better (P/E)
    assert _band(10, good=12, bad=45) == pytest.approx(1.0)
    assert _band(50, good=12, bad=45) == pytest.approx(-1.0)
    # higher is better (margin)
    assert _band(0.30, good=0.25, bad=0.02) == pytest.approx(1.0)
    assert _band(0.0, good=0.25, bad=0.02) == pytest.approx(-1.0)
    assert _band(None, 1, 2) is None


def test_fundamentals_declines_for_non_equity(settings, gold):
    ctx = build_context(gold, {Timeframe.D1: trending_closes(300, 0.1)})
    result = FundamentalsEngine(settings).analyse(ctx)
    assert result.skipped_reason is not None


# ------------------------------------------------------------- crash isolation


def test_engine_crash_is_contained(settings, gold):
    class Exploding(Engine):
        id = EngineId.TREND

        def _run(self, ctx):
            raise RuntimeError("deliberate blow-up")

    ctx = build_context(gold, {Timeframe.D1: trending_closes(300, 0.1)})
    result = Exploding().analyse(ctx)
    assert result.skipped_reason is not None
    assert "deliberate blow-up" in result.skipped_reason
    assert result.quality == 0.0
