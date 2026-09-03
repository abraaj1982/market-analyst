"""Resampling, forming-candle removal, data quality and the candle repository."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analyst.core.clock import UTC, ensure_utc_index
from analyst.core.enums import Timeframe
from analyst.data import quality
from analyst.data.resample import build_timeframes, drop_forming_candle, resample
from tests.conftest import make_frame


def hourly(n: int = 240, start: str = "2024-01-01 00:00") -> pd.DataFrame:
    frame = make_frame(100 + np.cumsum(np.random.default_rng(0).normal(0, 0.5, n)), start=start)
    return frame


def test_resample_anchors_to_the_clock():
    """4H bars must start at 00/04/08/12/16/20 UTC regardless of where the
    downloaded series happens to begin."""
    frame = hourly(240, start="2024-01-01 07:00")
    four_hour = resample(frame, Timeframe.H4)
    assert set(four_hour.index.hour) <= {0, 4, 8, 12, 16, 20}


def test_resample_preserves_ohlc_semantics():
    frame = hourly(24, start="2024-01-01 00:00")
    four_hour = resample(frame, Timeframe.H4)
    first = frame.iloc[0:4]
    assert float(four_hour["open"].iloc[0]) == pytest.approx(float(first["open"].iloc[0]))
    assert float(four_hour["close"].iloc[0]) == pytest.approx(float(first["close"].iloc[-1]))
    assert float(four_hour["high"].iloc[0]) == pytest.approx(float(first["high"].max()))
    assert float(four_hour["low"].iloc[0]) == pytest.approx(float(first["low"].min()))
    assert float(four_hour["volume"].iloc[0]) == pytest.approx(float(first["volume"].sum()))


def test_drop_forming_candle():
    """A bar stamped T is closed only once now >= T + timeframe."""
    frame = hourly(10, start="2024-01-01 00:00")
    last_open = frame.index[-1].to_pydatetime()

    mid_bar = last_open + pd.Timedelta(minutes=30)
    assert len(drop_forming_candle(frame, Timeframe.H1, mid_bar)) == len(frame) - 1

    after_close = last_open + pd.Timedelta(minutes=61)
    assert len(drop_forming_candle(frame, Timeframe.H1, after_close)) == len(frame)


def test_build_timeframes_marks_derived():
    frame = hourly(400)
    as_of = frame.index[-1].to_pydatetime() + pd.Timedelta(hours=5)
    built = build_timeframes(frame, Timeframe.H1, [Timeframe.H1, Timeframe.H4], as_of=as_of)
    assert built[Timeframe.H1][1] is False
    assert built[Timeframe.H4][1] is True


def test_build_timeframes_refuses_to_invent_detail():
    frame = hourly(200)
    built = build_timeframes(frame, Timeframe.H1, [Timeframe.M15], as_of=None)
    assert Timeframe.M15 not in built


def test_ensure_utc_index_deduplicates_and_sorts():
    frame = hourly(5)
    scrambled = pd.concat([frame.iloc[[3]], frame, frame.iloc[[1]]])
    clean = ensure_utc_index(scrambled)
    assert clean.index.is_monotonic_increasing
    assert clean.index.is_unique
    assert clean.index.tz is UTC or str(clean.index.tz) == "UTC"


# ------------------------------------------------------------------- quality


def test_quality_perfect_data_scores_high(settings):
    frame = hourly(400)
    as_of = frame.index[-1].to_pydatetime() + pd.Timedelta(minutes=30)
    score, issues = quality.assess_timeframe(frame, Timeframe.H1, settings.data_quality, as_of)
    assert score > 0.9
    assert not issues


def test_quality_flags_broken_ohlc(settings):
    frame = hourly(400).copy()
    frame.iloc[-5, frame.columns.get_loc("high")] = frame["low"].iloc[-5] - 10
    as_of = frame.index[-1].to_pydatetime() + pd.Timedelta(minutes=30)
    score, issues = quality.assess_timeframe(frame, Timeframe.H1, settings.data_quality, as_of)
    assert score < 1.0
    assert any("OHLC" in issue for issue in issues)


def test_quality_flags_staleness(settings):
    frame = hourly(400)
    stale_as_of = frame.index[-1].to_pydatetime() + pd.Timedelta(days=3)
    score, issues = quality.assess_timeframe(frame, Timeframe.H1, settings.data_quality, stale_as_of)
    assert score < 0.9
    assert any("stale" in issue for issue in issues)


def test_quality_empty_frame_is_zero(settings):
    score, issues = quality.assess_timeframe(
        pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
        Timeframe.H1, settings.data_quality,
    )
    assert score == 0.0
    assert issues


def test_overall_quality_is_dragged_by_the_worst_timeframe(settings):
    good = hourly(400)
    as_of = good.index[-1].to_pydatetime() + pd.Timedelta(minutes=30)
    bad = good.head(30)  # far too few bars
    report = quality.assess({Timeframe.H1: good, Timeframe.H4: bad}, settings.data_quality, as_of)
    assert report.score < min(1.0, report.per_timeframe[Timeframe.H1])


# ---------------------------------------------------------------- repository


def test_repository_upsert_is_idempotent(repository, gold):
    first = repository.load(gold, Timeframe.H1, 300)
    count_1, _ = repository.coverage(gold.symbol, Timeframe.H1)
    repository.load(gold, Timeframe.H1, 300)
    count_2, newest = repository.coverage(gold.symbol, Timeframe.H1)
    assert count_1 == count_2 == len(first)
    assert newest is not None


def test_repository_falls_back_to_cache(repository, gold):
    from analyst.core.errors import DataUnavailableError
    from analyst.data.providers.base import PriceProvider

    repository.load(gold, Timeframe.H1, 300)

    class DeadProvider(PriceProvider):
        name = "dead"
        native_timeframes = (Timeframe.H1,)

        def fetch(self, instrument, timeframe, bars):
            raise DataUnavailableError("المزود معطّل")

    from analyst.data.repository import CandleRepository

    offline_repo = CandleRepository([DeadProvider()])
    cached = offline_repo.load(gold, Timeframe.H1, 300)
    assert not cached.empty
