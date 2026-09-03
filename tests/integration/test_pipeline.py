"""End-to-end behaviour: the pipeline, persistence, the API and the CLI."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analyst.core.config import active_instruments, load_gates
from analyst.core.enums import Direction, Grade, Timeframe
from analyst.data.context import ContextBuilder
from analyst.data.providers.base import PriceProvider
from analyst.data.providers.calendar import EconomicCalendar
from analyst.data.repository import CandleRepository
from analyst.pipeline import Pipeline
from analyst.storage.analyses import history_for, latest_analyses, save_analysis
from tests.conftest import ANCHOR


class TrendingProvider(PriceProvider):
    """A clean, multi-timeframe-aligned trend — the shape a real setup has."""

    name = "trending"
    native_timeframes = (Timeframe.M15, Timeframe.H1, Timeframe.D1, Timeframe.W1)

    def __init__(self, end: pd.Timestamp, drift: float, seed: int = 42) -> None:
        self.end, self.drift, self.seed = end, drift, seed

    def fetch(self, instrument, timeframe, bars):
        rng = np.random.default_rng(self.seed)
        n = bars
        t = np.arange(n, dtype=float)
        step = self.drift * (timeframe.minutes / 60.0)
        log_price = step * t + 0.01 * np.sin(t / 45.0) + np.cumsum(rng.normal(0, 0.0009, n))
        close = 2400 * np.exp(log_price - log_price[-1])
        open_ = np.concatenate([[close[0]], close[:-1]])
        pad = np.abs(close - open_) + close * 0.0009
        return pd.DataFrame(
            {
                "open": open_,
                "high": np.maximum(open_, close) + pad * rng.uniform(0.1, 0.5, n),
                "low": np.minimum(open_, close) - pad * rng.uniform(0.1, 0.5, n),
                "close": close,
                "volume": 1e6 * (1 + 0.4 * np.sign(close - open_) + rng.uniform(0, 0.3, n)),
            },
            index=pd.date_range(end=self.end, periods=n, freq=timeframe.pandas_rule, tz="UTC"),
        )


def make_pipeline(provider, settings) -> Pipeline:
    builder = ContextBuilder(CandleRepository([provider]), settings, calendar=EconomicCalendar())
    return Pipeline(builder, settings, load_gates())


# ------------------------------------------------------------------ pipeline


def test_pipeline_produces_a_complete_result(pipeline, gold):
    result = pipeline.analyse(gold, as_of=ANCHOR.to_pydatetime())

    assert result.symbol == "XAUUSD"
    assert 0.0 <= result.confidence <= 1.0
    assert result.report and len(result.report) > 400
    assert result.gates, "gates must always be evaluated"
    assert len(result.engines) == 9, "every engine must report, even to decline"
    assert result.code_version and result.config_version


def test_pipeline_grades_a_clean_trend_above_noise(settings, gold):
    trend = make_pipeline(TrendingProvider(ANCHOR, 0.0006), settings)
    trend_result = trend.analyse(gold, as_of=ANCHOR.to_pydatetime())

    flat = make_pipeline(TrendingProvider(ANCHOR, 0.0, seed=7), settings)
    flat_result = flat.analyse(gold, as_of=ANCHOR.to_pydatetime())

    assert trend_result.confidence > flat_result.confidence
    assert abs(trend_result.breakdown.raw_signed_score) > abs(flat_result.breakdown.raw_signed_score)


def test_pipeline_direction_follows_the_trend(settings, gold):
    up = make_pipeline(TrendingProvider(ANCHOR, 0.0006), settings).analyse(
        gold, as_of=ANCHOR.to_pydatetime()
    )
    down = make_pipeline(TrendingProvider(ANCHOR, -0.0006), settings).analyse(
        gold, as_of=ANCHOR.to_pydatetime()
    )
    assert up.direction is Direction.BULLISH
    assert down.direction is Direction.BEARISH


def test_blocked_setup_is_downgraded(pipeline, gold):
    """A high score with a failed hard gate must never be reported as A or A+."""
    result = pipeline.analyse(gold, as_of=ANCHOR.to_pydatetime())
    if result.blocking_failures:
        assert result.grade not in (Grade.A_PLUS, Grade.A)
        assert result.is_actionable is False


def test_engines_never_crash_the_run(pipeline, instruments):
    for instrument in instruments.values():
        result = pipeline.analyse(instrument, as_of=ANCHOR.to_pydatetime())
        assert result.confidence == result.confidence  # not NaN
        for engine in result.engines:
            assert engine.skipped_reason is None or isinstance(engine.skipped_reason, str)


# --------------------------------------------------------------- persistence


def test_analysis_round_trip(pipeline, gold):
    result = pipeline.analyse(gold, as_of=ANCHOR.to_pydatetime())
    row_id = save_analysis(result)
    assert row_id > 0

    stored = history_for("XAUUSD", limit=1)[0]
    assert stored.symbol == result.symbol
    assert stored.confidence == pytest.approx(result.confidence)
    assert stored.payload["breakdown"]["raw_signed_score"] == pytest.approx(
        result.breakdown.raw_signed_score
    )


def test_latest_analyses_is_one_row_per_symbol(pipeline, instruments):
    for instrument in list(instruments.values())[:3]:
        for _ in range(2):
            save_analysis(pipeline.analyse(instrument, as_of=ANCHOR.to_pydatetime()))
    rows = latest_analyses()
    assert len({r.symbol for r in rows}) == len(rows)


# ---------------------------------------------------------------------- API


def test_api_endpoints(pipeline, gold):
    from fastapi.testclient import TestClient

    from analyst.api.app import create_app

    save_analysis(pipeline.analyse(gold, as_of=ANCHOR.to_pydatetime()))
    app = create_app(offline=True, schedule=False)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert len(client.get("/api/instruments").json()) == len(active_instruments())

        analyses = client.get("/api/analyses").json()
        assert any(row["symbol"] == "XAUUSD" for row in analyses)

        detail = client.get("/api/analysis/XAUUSD").json()
        assert detail["report"]
        assert detail["payload"]["gates"]

        assert client.get("/api/analysis/NOPE").status_code == 404
        assert client.get("/api/candles/XAUUSD?timeframe=nope").status_code == 400
        assert client.get("/").status_code == 200


# ---------------------------------------------------------------------- CLI


def test_cli_offline_analyze_runs():
    from typer.testing import CliRunner

    from analyst.cli import app

    result = CliRunner().invoke(app, ["analyze", "--offline", "--no-alert", "XAUUSD"])
    assert result.exit_code == 0, result.output


# --------------------------------------------------- manual company register


def test_manual_company_api_flow():
    from fastapi.testclient import TestClient

    from analyst.api.app import create_app

    app = create_app(offline=True, schedule=False)
    with TestClient(app) as client:
        created = client.post("/api/companies", json={
            "symbol": "bkmb", "name": "Bank Muscat", "sector": "Banking",
            "currency": "OMR", "price": 0.240, "dividend_per_share": 0.018,
            "eps": 0.030, "dividend_years_paid": 7,
        })
        assert created.status_code == 200
        assert created.json()["symbol"] == "BKMB"

        assert client.post("/api/companies", json={"name": "No symbol"}).status_code == 400

        client.post("/api/companies/BKMB/news",
                    json={"headline": "Bank Muscat raises dividend after record profit",
                          "source": "MSX"})
        client.post("/api/companies/BKMB/news",
                    json={"headline": "الشركة تعلن عن ترسية عقد جديد", "source": "Argaam"})

        news = client.get("/api/companies/BKMB/news").json()
        assert len(news) == 2
        assert all(n["sentiment"] > 0 for n in news)

        assessment = client.get("/api/companies/BKMB/assessment").json()
        assert assessment["symbol"] == "BKMB"
        assert assessment["report"]
        # the manual path never emits a trade plan
        assert "risk" not in assessment

        preview = client.post("/api/sentiment/preview",
                              json={"headline": "dividend cut and net loss"}).json()
        assert preview["sentiment"] < 0
        assert preview["matched_terms"]["negative"]

        assert client.get("/api/companies/NOPE/assessment").status_code == 404
        assert client.delete("/api/companies/BKMB").status_code == 200
        assert client.get("/api/companies/BKMB/assessment").status_code == 404


def test_run_endpoint_accepts_every_body_shape():
    """The dashboard's Run button posts `{}`; a bare list annotation rejects it."""
    from fastapi.testclient import TestClient

    from analyst.api.app import create_app

    with TestClient(create_app(offline=True, schedule=False)) as client:
        assert client.post("/api/run").status_code == 200
        assert client.post("/api/run", json={}).status_code == 200
        scoped = client.post("/api/run", json={"symbols": ["XAUUSD"]})
        assert scoped.status_code == 200
        assert scoped.json()["analysed"] == 1


def test_legacy_schema_is_migrated_in_place(tmp_path, monkeypatch):
    """A pre-1.1.0 database must keep working after the column rename."""
    import sqlite3

    from analyst.core.config import reset_caches
    from analyst.storage.db import init_db, reset_engine, session_scope
    from analyst.storage.models import Analysis

    db = tmp_path / "legacy.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE analyses (id INTEGER PRIMARY KEY, symbol VARCHAR(32) NOT NULL,"
        " name_ar VARCHAR(128), market VARCHAR(16), as_of DATETIME NOT NULL,"
        " spot FLOAT NOT NULL, direction INTEGER NOT NULL, confidence FLOAT NOT NULL,"
        " grade VARCHAR(12) NOT NULL, regime VARCHAR(24), actionable BOOLEAN,"
        " report_ar TEXT, payload JSON, config_version VARCHAR(32),"
        " code_version VARCHAR(64), created_at DATETIME)"
    )
    con.execute(
        "INSERT INTO analyses (symbol, name_ar, as_of, spot, direction, confidence,"
        " grade, report_ar) VALUES ('XAUUSD','Gold','2026-01-01',2400,1,0.8,'A','old report')"
    )
    con.commit()
    con.close()

    monkeypatch.setenv("ANALYST_DATABASE_URL", f"sqlite:///{db}")
    reset_caches()
    reset_engine()
    init_db()

    with session_scope() as session:
        row = session.query(Analysis).first()
        assert row.name == "Gold"
        assert row.report == "old report"

    reset_engine()
    reset_caches()
