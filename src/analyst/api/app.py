"""FastAPI application: JSON API plus the Arabic RTL dashboard.

The API deliberately returns the *stored* analyses rather than recomputing on
request. A dashboard refresh must never trigger nine live analyses — that would
burn free-tier rate limits and make the displayed numbers depend on who happened
to open the page. Recomputation happens on the scheduler's clock, or explicitly
via `POST /api/run`.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from analyst.core.clock import now_utc, to_utc
from analyst.core.config import WEB_DIR, active_instruments, load_settings
from analyst.core.enums import Timeframe
from analyst.storage.analyses import history_for, latest_analyses
from analyst.storage.db import init_db
from analyst.tracking import stats as stats_module
from analyst.version import code_version

log = logging.getLogger(__name__)


def create_app(offline: bool = False, schedule: bool = True) -> FastAPI:
    settings = load_settings()
    init_db()

    app = FastAPI(title="محلل الأسواق", version=code_version(), docs_url="/api/docs")
    state: dict[str, Any] = {"service": None, "scheduler": None}

    @app.on_event("startup")
    def _startup() -> None:
        from analyst.runner import build_service

        state["service"] = build_service(offline=offline)
        if schedule:
            from analyst.scheduler.jobs import start_scheduler

            state["scheduler"] = start_scheduler(state["service"])
            log.info("الجدولة الدورية مفعّلة")

    @app.on_event("shutdown")
    def _shutdown() -> None:
        scheduler = state.get("scheduler")
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    # ---------------------------------------------------------------- API

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": code_version(),
            "config_version": settings.version,
            "profile": settings.profile,
            "timezone": settings.timezone.display,
            "offline": offline,
            "server_time_utc": now_utc().isoformat(),
        }

    @app.get("/api/instruments")
    def instruments() -> list[dict]:
        return [
            {
                "symbol": i.symbol, "name_ar": i.name_ar, "market": i.market.value,
                "asset_class": i.asset_class.value, "currency": i.currency,
                "shortable": i.shortable,
                "timeframes": [tf.value for tf in i.supported_timeframes],
            }
            for i in active_instruments()
        ]

    @app.get("/api/analyses")
    def analyses() -> list[dict]:
        """Latest analysis per symbol, ranked with actionable setups first."""
        rows = latest_analyses(limit=100)
        out = [_summarise(row) for row in rows]
        out.sort(key=lambda r: (r["actionable"], r["confidence"]), reverse=True)
        return out

    @app.get("/api/analysis/{symbol}")
    def analysis(symbol: str) -> dict:
        rows = history_for(symbol.upper(), limit=1)
        if not rows:
            raise HTTPException(404, f"لا يوجد تحليل محفوظ للرمز {symbol}")
        row = rows[0]
        return {**_summarise(row), "report_ar": row.report_ar, "payload": row.payload}

    @app.get("/api/history/{symbol}")
    def history(symbol: str, limit: int = Query(200, le=1000)) -> list[dict]:
        return [
            {
                "as_of": to_utc(r.as_of).isoformat(),
                "confidence": r.confidence,
                "direction": r.direction,
                "grade": r.grade,
                "spot": r.spot,
            }
            for r in reversed(history_for(symbol.upper(), limit))
        ]

    @app.get("/api/candles/{symbol}")
    def candles(
        symbol: str,
        timeframe: str = Query("4h"),
        bars: int = Query(400, le=2000),
    ) -> list[dict]:
        try:
            tf = Timeframe(timeframe)
        except ValueError:
            raise HTTPException(400, f"إطار زمني غير مدعوم: {timeframe}") from None

        service = state.get("service")
        if service is None:
            raise HTTPException(503, "الخدمة لم تُهيّأ بعد")

        frame = service.repository.read(symbol.upper(), tf, bars)
        if frame.empty and tf is Timeframe.H4:
            # 4H is derived, so it is not stored; rebuild it from stored 1H bars
            from analyst.data.resample import resample

            base = service.repository.read(symbol.upper(), Timeframe.H1, bars * 4)
            if not base.empty:
                frame = resample(base, tf).tail(bars)
        if frame.empty:
            return []
        return [
            {
                "time": int(ts.timestamp()),
                "open": float(r.open), "high": float(r.high),
                "low": float(r.low), "close": float(r.close),
                "volume": float(r.volume),
            }
            for ts, r in frame.iterrows()
        ]

    @app.get("/api/stats")
    def performance(symbol: str | None = None) -> dict:
        result = stats_module.compute(symbol)
        return {
            "sample": result.sample,
            "open_count": result.open_count,
            "wins": result.wins,
            "losses": result.losses,
            "expired": result.expired,
            "win_rate": result.win_rate,
            "win_rate_ci": result.win_rate_ci,
            "expectancy_r": result.expectancy_r,
            "avg_win_r": result.avg_win_r,
            "avg_loss_r": result.avg_loss_r,
            "profit_factor": result.profit_factor,
            "avg_mfe_r": result.avg_mfe_r,
            "avg_mae_r": result.avg_mae_r,
            "by_grade": result.by_grade,
            "is_significant": result.is_significant,
            "headline_ar": result.headline_ar,
        }

    @app.post("/api/run")
    def run(symbols: list[str] | None = None) -> dict:
        service = state.get("service")
        if service is None:
            raise HTTPException(503, "الخدمة لم تُهيّأ بعد")
        results = service.run_once(symbols=symbols)
        return {"analysed": len(results), "at": now_utc().isoformat()}

    # ---------------------------------------------------------- dashboard

    if WEB_DIR.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

        @app.get("/")
        def dashboard() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")
    else:
        @app.get("/")
        def missing() -> JSONResponse:
            return JSONResponse({"error": "مجلد web غير موجود"}, status_code=500)

    return app


def _summarise(row) -> dict:
    payload = row.payload or {}
    breakdown = payload.get("breakdown", {})
    gates = payload.get("gates", [])
    return {
        "symbol": row.symbol,
        "name_ar": row.name_ar,
        "market": row.market,
        "as_of": to_utc(row.as_of).isoformat(),
        "spot": row.spot,
        "direction": row.direction,
        "confidence": row.confidence,
        "grade": row.grade,
        "regime": row.regime,
        "actionable": bool(row.actionable),
        "risk": payload.get("risk"),
        "contributions": breakdown.get("contributions", []),
        "raw_signed_score": breakdown.get("raw_signed_score"),
        "calibrated_consensus": breakdown.get("calibrated_consensus"),
        "coherence": breakdown.get("coherence"),
        "data_quality": breakdown.get("data_quality"),
        "news_factor": breakdown.get("news_factor"),
        "regime_fit": breakdown.get("regime_fit"),
        "active_engines": breakdown.get("active_engines"),
        "coverage_ratio": (
            round(breakdown.get("total_effective_weight", 0) / breakdown["available_weight"], 4)
            if breakdown.get("available_weight") else 0.0
        ),
        "blocking_failures": [
            {"label_ar": g["label_ar"], "detail_ar": g.get("detail_ar", "")}
            for g in gates
            if g.get("blocking") and g.get("status") != "passed"
        ],
        "engines": payload.get("engines", []),
    }
