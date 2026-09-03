"""Yahoo Finance provider (free, no API key).

Covers metals futures, FX pairs, US equities and indices — the three markets on
the watchlist that have reliable free data. Fundamentals come from the same
source, which keeps the whole system dependency-light and key-free.

Known limits, handled explicitly rather than hidden:
  * intraday history is capped (60d for 15m, ~730d for 1h) — we request what is
    allowed and let the quality layer judge whether it is enough
  * spot FX pairs (`EURUSD=X`) report no meaningful volume; the volume engine
    detects this and steps aside instead of scoring noise
  * no 4H interval exists; it is built by resampling 1H
"""
from __future__ import annotations

import logging

import pandas as pd

from analyst.core.clock import ensure_utc_index
from analyst.core.enums import Timeframe
from analyst.core.errors import DataUnavailableError, ProviderError
from analyst.core.models import Instrument
from analyst.data.providers.base import FundamentalsProvider, PriceProvider

log = logging.getLogger(__name__)

#: Yahoo interval string per timeframe (4H intentionally absent — it is derived).
_INTERVAL = {
    Timeframe.M15: "15m",
    Timeframe.H1: "1h",
    Timeframe.D1: "1d",
    Timeframe.W1: "1wk",
}

#: Yahoo refuses ranges longer than these per interval.
_MAX_DAYS = {Timeframe.M15: 58, Timeframe.H1: 720, Timeframe.D1: 7300, Timeframe.W1: 7300}


class YahooProvider(PriceProvider):
    name = "yahoo"
    native_timeframes = (Timeframe.M15, Timeframe.H1, Timeframe.D1, Timeframe.W1)

    def __init__(self, fallbacks: dict[str, list[str]] | None = None) -> None:
        self._fallbacks = fallbacks or {}

    def fetch(self, instrument: Instrument, timeframe: Timeframe, bars: int) -> pd.DataFrame:
        if timeframe not in _INTERVAL:
            raise ProviderError(f"{self.name}: does not serve the {timeframe.value} interval")

        candidates = [instrument.provider_symbol, *self._fallbacks.get(instrument.symbol, [])]
        errors: list[str] = []
        for ticker in candidates:
            try:
                frame = self._download(ticker, timeframe, bars)
            except Exception as exc:  # provider/transport level
                errors.append(f"{ticker}: {exc}")
                continue
            if frame is not None and not frame.empty:
                if ticker != instrument.provider_symbol:
                    log.warning(
                        "%s: used fallback ticker %s instead of %s",
                        instrument.symbol, ticker, instrument.provider_symbol,
                    )
                return frame
            errors.append(f"{ticker}: empty")

        raise DataUnavailableError(
            f"{instrument.symbol} @ {timeframe.value}: no source returned data ({'; '.join(errors)})"
        )

    def _download(self, ticker: str, timeframe: Timeframe, bars: int) -> pd.DataFrame:
        import yfinance as yf

        days_needed = max(5, int(bars * timeframe.minutes / (60 * 24) * 1.8) + 5)
        period_days = min(days_needed, _MAX_DAYS[timeframe])

        raw = yf.download(
            ticker,
            period=f"{period_days}d",
            interval=_INTERVAL[timeframe],
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if raw is None or raw.empty:
            return pd.DataFrame()
        return self._normalise(raw).tail(bars)

    @staticmethod
    def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
        """Flatten yfinance's shifting column shapes into the canonical schema."""
        if isinstance(raw.columns, pd.MultiIndex):
            # single-ticker download still returns (field, ticker) in newer versions
            raw = raw.droplevel(-1, axis=1)
        raw = raw.rename(columns=str.lower)
        if "volume" not in raw.columns:
            raw["volume"] = 0.0
        frame = raw.loc[:, ["open", "high", "low", "close", "volume"]].astype(float)
        frame = ensure_utc_index(frame)
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        frame["volume"] = frame["volume"].fillna(0.0)
        return frame


class YahooFundamentals(FundamentalsProvider):
    """Valuation, profitability, leverage, dividends and next earnings date."""

    name = "yahoo_fundamentals"

    def fetch(self, instrument: Instrument) -> dict:
        import yfinance as yf

        try:
            ticker = yf.Ticker(instrument.provider_symbol)
            info = ticker.info or {}
        except Exception as exc:
            raise ProviderError(f"fundamentals {instrument.symbol}: {exc}") from exc

        out = {
            "trailing_pe": _num(info.get("trailingPE")),
            "forward_pe": _num(info.get("forwardPE")),
            "price_to_book": _num(info.get("priceToBook")),
            "peg_ratio": _num(info.get("pegRatio")),
            "profit_margin": _num(info.get("profitMargins")),
            "return_on_equity": _num(info.get("returnOnEquity")),
            "revenue_growth": _num(info.get("revenueGrowth")),
            "earnings_growth": _num(info.get("earningsGrowth")),
            "debt_to_equity": _num(info.get("debtToEquity")),
            "current_ratio": _num(info.get("currentRatio")),
            "dividend_yield": _num(info.get("dividendYield")),
            "payout_ratio": _num(info.get("payoutRatio")),
            "beta": _num(info.get("beta")),
            "market_cap": _num(info.get("marketCap")),
            "sector": info.get("sector"),
        }
        out["next_earnings"] = self._next_earnings(ticker)
        return out

    @staticmethod
    def _next_earnings(ticker) -> pd.Timestamp | None:
        try:
            cal = ticker.calendar
        except Exception:
            return None
        if cal is None:
            return None
        try:
            if isinstance(cal, dict):
                dates = cal.get("Earnings Date") or []
                return pd.Timestamp(dates[0]).tz_localize("UTC") if dates else None
            if isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
                return pd.Timestamp(cal.loc["Earnings Date"].iloc[0]).tz_localize("UTC")
        except Exception:
            return None
        return None


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # reject NaN
