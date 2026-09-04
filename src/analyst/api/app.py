"""FastAPI application: JSON API plus the Arabic RTL dashboard.

The API deliberately returns the *stored* analyses rather than recomputing on
request. A dashboard refresh must never trigger nine live analyses — that would
burn free-tier rate limits and make the displayed numbers depend on who happened
to open the page. Recomputation happens on the scheduler's clock, or explicitly
via `POST /api/run`.
"""
from __future__ import annotations

import base64
import hmac
import logging
import os
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from analyst.core.clock import now_utc, to_utc
from analyst.core.config import WEB_DIR, active_instruments, load_settings
from analyst.core.enums import Timeframe
from analyst.storage.analyses import history_for, latest_analyses
from analyst.storage.db import init_db
from analyst.tracking import stats as stats_module
from analyst.version import code_version

log = logging.getLogger(__name__)


class _BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gate every route but /api/health behind HTTP Basic Auth.

    Only installed when DASHBOARD_USER and DASHBOARD_PASSWORD are set, so a
    local, non-deployed run stays unauthenticated exactly as before.
    """

    def __init__(self, app: FastAPI, username: str, password: str) -> None:
        super().__init__(app)
        self._username = username
        self._password = password

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/api/health":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                user = pwd = ""
            if hmac.compare_digest(user, self._username) and hmac.compare_digest(
                pwd, self._password
            ):
                return await call_next(request)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Market Analyst"'})


def create_app(offline: bool = False, schedule: bool = True) -> FastAPI:
    settings = load_settings()
    init_db()

    app = FastAPI(title="Market Analyst", version=code_version(), docs_url="/api/docs")
    dashboard_user, dashboard_password = os.getenv("DASHBOARD_USER"), os.getenv("DASHBOARD_PASSWORD")
    if dashboard_user and dashboard_password:
        app.add_middleware(_BasicAuthMiddleware, username=dashboard_user, password=dashboard_password)
    state: dict[str, Any] = {"service": None, "scheduler": None}

    @app.on_event("startup")
    def _startup() -> None:
        from analyst.runner import build_service

        state["service"] = build_service(offline=offline)
        if schedule:
            from analyst.scheduler.jobs import start_scheduler

            state["scheduler"] = start_scheduler(state["service"])
            log.info("Scheduler enabled")

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
                "symbol": i.symbol, "name": i.name, "market": i.market.value,
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
            raise HTTPException(404, f"No stored analysis for {symbol}")
        row = rows[0]
        return {**_summarise(row), "report": row.report, "payload": row.payload}

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

    def _read_frame(symbol: str, tf: Timeframe, bars: int):
        service = state.get("service")
        if service is None:
            raise HTTPException(503, "Service is not initialised yet")

        frame = service.repository.read(symbol.upper(), tf, bars)
        if frame.empty and tf is Timeframe.H4:
            # 4H is derived, so it is not stored; rebuild it from stored 1H bars
            from analyst.data.resample import resample

            base = service.repository.read(symbol.upper(), Timeframe.H1, bars * 4)
            if not base.empty:
                frame = resample(base, tf).tail(bars)
        return frame

    @app.get("/api/candles/{symbol}")
    def candles(
        symbol: str,
        timeframe: str = Query("4h"),
        bars: int = Query(400, le=2000),
    ) -> list[dict]:
        try:
            tf = Timeframe(timeframe)
        except ValueError:
            raise HTTPException(400, f"Unsupported timeframe: {timeframe}") from None

        frame = _read_frame(symbol, tf, bars)
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

    @app.get("/api/indicators/{symbol}")
    def indicators(
        symbol: str,
        timeframe: str = Query("4h"),
        bars: int = Query(400, le=2000),
    ) -> dict:
        """Moving averages and Ichimoku Kinko Hyo, computed server-side so the
        chart never carries its own copy of the math -- it only plots numbers
        the same engines already trust.
        """
        try:
            tf = Timeframe(timeframe)
        except ValueError:
            raise HTTPException(400, f"Unsupported timeframe: {timeframe}") from None

        from analyst.indicators.trend import ema, ichimoku, sma

        frame = _read_frame(symbol, tf, bars)
        if frame.empty:
            return {"sma20": [], "sma50": [], "ema20": [], "ichimoku": {}}

        def series_points(s) -> list[dict]:
            return [
                {"time": int(ts.timestamp()), "value": float(v)}
                for ts, v in s.items()
                if v == v  # drops NaN (warm-up period)
            ]

        cloud = ichimoku(frame["high"], frame["low"], frame["close"])
        return {
            "sma20": series_points(sma(frame["close"], 20)),
            "sma50": series_points(sma(frame["close"], 50)),
            "ema20": series_points(ema(frame["close"], 20)),
            "ichimoku": {
                "tenkan": series_points(cloud["tenkan"]),
                "kijun": series_points(cloud["kijun"]),
                "span_a": series_points(cloud["span_a"]),
                "span_b": series_points(cloud["span_b"]),
            },
        }

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
            "headline": result.headline,
        }

    @app.post("/api/run")
    def run(payload: dict | None = Body(default=None)) -> dict:
        """Trigger an analysis run. Body is optional: {"symbols": ["XAUUSD"]}.

        Typed as a dict rather than `list[str]` because FastAPI would otherwise
        require the request body to *be* a JSON array, and an empty object from
        the dashboard's "Run analysis" button would be rejected as malformed.
        """
        service = state.get("service")
        if service is None:
            raise HTTPException(503, "Service is not initialised yet")
        symbols = (payload or {}).get("symbols") or None
        results = service.run_once(symbols=symbols)
        return {"analysed": len(results), "at": now_utc().isoformat()}

    # ------------------------------------------- manual company register

    @app.get("/api/companies")
    def companies() -> list[dict]:
        """The manual register: companies with no price feed."""
        from analyst.manual.service import list_companies

        return list_companies()

    @app.post("/api/companies")
    def save_company(payload: dict = Body(...)) -> dict:
        from analyst.manual.service import upsert_company

        symbol = str(payload.get("symbol", "")).strip()
        if not symbol:
            raise HTTPException(400, "symbol is required")
        try:
            row_id = upsert_company(symbol, payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": row_id, "symbol": symbol.upper()}

    @app.delete("/api/companies/{symbol}")
    def remove_company(symbol: str) -> dict:
        from analyst.manual.service import delete_company

        if not delete_company(symbol):
            raise HTTPException(404, f"{symbol} is not in the manual register")
        return {"deleted": symbol.upper()}

    @app.get("/api/companies/{symbol}/assessment")
    def company_assessment(symbol: str) -> dict:
        from analyst.manual.service import assess_symbol

        assessment = assess_symbol(symbol)
        if assessment is None:
            raise HTTPException(404, f"{symbol} is not in the manual register")
        return assessment.model_dump(mode="json")

    @app.get("/api/companies/{symbol}/news")
    def company_news(symbol: str) -> list[dict]:
        from analyst.manual.service import news_rows

        return news_rows(symbol)

    @app.post("/api/companies/{symbol}/news")
    def add_company_news(symbol: str, payload: dict = Body(...)) -> dict:
        from datetime import datetime

        from analyst.manual.service import add_news

        published = payload.get("published_at")
        when = None
        if published:
            try:
                when = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(400, f"Invalid published_at: {published}") from exc
        try:
            news_id = add_news(
                symbol,
                headline=str(payload.get("headline", "")),
                published_at=when,
                source=str(payload.get("source", "")),
                manual_sentiment=payload.get("manual_sentiment"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": news_id}

    @app.delete("/api/news/{news_id}")
    def remove_news(news_id: int) -> dict:
        from analyst.manual.service import delete_news

        if not delete_news(news_id):
            raise HTTPException(404, f"News item {news_id} not found")
        return {"deleted": news_id}

    @app.post("/api/sentiment/preview")
    def sentiment_preview(payload: dict = Body(...)) -> dict:
        """Score a headline without storing it — powers the live preview."""
        from analyst.manual.lexicon import score_text

        sentiment, matched = score_text(str(payload.get("headline", "")))
        return {"sentiment": sentiment, "matched_terms": matched}

    # ---------------------------------------------------------- dashboard

    if WEB_DIR.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

        @app.get("/")
        def dashboard() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")
    else:
        @app.get("/")
        def missing() -> JSONResponse:
            return JSONResponse({"error": "web/ directory is missing"}, status_code=500)

    return app


def _summarise(row) -> dict:
    payload = row.payload or {}
    breakdown = payload.get("breakdown", {})
    gates = payload.get("gates", [])
    return {
        "symbol": row.symbol,
        "name": row.name,
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
            {"label": g["label"], "detail": g.get("detail", "")}
            for g in gates
            if g.get("blocking") and g.get("status") != "passed"
        ],
        "engines": payload.get("engines", []),
    }
