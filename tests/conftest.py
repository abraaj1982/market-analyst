"""Shared fixtures.

Every test runs against a temporary SQLite file and the synthetic provider, so
the suite is fully offline, deterministic, and leaves no state behind.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analyst.core.clock import UTC
from analyst.core.config import active_instruments, load_gates, load_settings, reset_caches
from analyst.core.models import Instrument
from analyst.data.context import ContextBuilder
from analyst.data.providers.calendar import EconomicCalendar
from analyst.data.providers.synthetic import SyntheticFundamentals, SyntheticProvider
from analyst.data.repository import CandleRepository
from analyst.pipeline import Pipeline
from analyst.storage.db import init_db, reset_engine

ANCHOR = pd.Timestamp("2026-06-01 12:00", tz="UTC")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    reset_engine()
    reset_caches()
    init_db()
    yield
    reset_engine()
    reset_caches()


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def instruments() -> dict[str, Instrument]:
    return {i.symbol: i for i in active_instruments()}


@pytest.fixture
def gold(instruments) -> Instrument:
    return instruments["XAUUSD"]


@pytest.fixture
def repository() -> CandleRepository:
    return CandleRepository([SyntheticProvider(anchor_end=ANCHOR)])


@pytest.fixture
def context_builder(repository, settings) -> ContextBuilder:
    return ContextBuilder(
        repository=repository,
        settings=settings,
        fundamentals=SyntheticFundamentals(),
        calendar=EconomicCalendar(),
    )


@pytest.fixture
def pipeline(context_builder, settings) -> Pipeline:
    return Pipeline(context_builder, settings, load_gates())


@pytest.fixture
def gold_context(context_builder, gold):
    return context_builder.build(gold, as_of=ANCHOR.to_pydatetime())


def make_frame(
    closes: list[float] | np.ndarray,
    start: str = "2024-01-01",
    freq: str = "h",
    wick: float = 0.002,
    volume: float = 1_000_000.0,
) -> pd.DataFrame:
    """Build a valid OHLCV frame from a close series.

    Wick size is a fixed fraction of *price*, not of the candle body. Scaling
    wicks with body size looks natural but silently breaks pivot detection: a
    large impulse candle then grows a wick that overshoots its neighbours, so
    genuine swing lows stop being local minima and structure tests fail for
    reasons that have nothing to do with the code under test.
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.date_range(start=start, periods=n, freq=freq, tz=UTC)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    pad = np.abs(closes) * wick
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + pad,
            "low": np.minimum(opens, closes) - pad,
            "close": closes,
            "volume": np.full(n, volume),
        },
        index=idx,
    )
