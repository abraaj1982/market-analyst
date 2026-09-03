"""Stooq CSV fallback (free, no key, daily and hourly).

Used when Yahoo returns nothing — an outage on one free source should degrade
coverage, not stop the system. Stooq serves plain CSV over HTTPS, so there is no
SDK and no auth to manage.
"""
from __future__ import annotations

import io

import httpx
import pandas as pd

from analyst.core.clock import ensure_utc_index
from analyst.core.enums import Timeframe
from analyst.core.errors import DataUnavailableError, ProviderError
from analyst.core.models import Instrument
from analyst.data.providers.base import PriceProvider

_BASE = "https://stooq.com/q/d/l/"

#: Stooq interval codes. Only daily and weekly are dependable on the free feed.
_INTERVAL = {Timeframe.D1: "d", Timeframe.W1: "w"}

#: Internal symbol -> Stooq ticker. Stooq uses its own naming for FX and metals.
_SYMBOL_MAP = {
    "XAUUSD": "xauusd", "XAGUSD": "xagusd",
    "EURUSD": "eurusd", "GBPUSD": "gbpusd", "USDJPY": "usdjpy",
    "SPX": "^spx", "NDX": "^ndx",
}


class StooqProvider(PriceProvider):
    name = "stooq"
    native_timeframes = (Timeframe.D1, Timeframe.W1)

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def fetch(self, instrument: Instrument, timeframe: Timeframe, bars: int) -> pd.DataFrame:
        if timeframe not in _INTERVAL:
            raise ProviderError(f"{self.name}: does not serve the {timeframe.value} interval")
        ticker = _SYMBOL_MAP.get(instrument.symbol, instrument.provider_symbol.lower())

        try:
            resp = httpx.get(
                _BASE,
                params={"s": ticker, "i": _INTERVAL[timeframe]},
                timeout=self.timeout,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"stooq {ticker}: {exc}") from exc

        text = resp.text.strip()
        if not text or text.lower().startswith("no data"):
            raise DataUnavailableError(f"stooq {ticker}: no data")

        frame = pd.read_csv(io.StringIO(text))
        frame.columns = [c.strip().lower() for c in frame.columns]
        if "date" not in frame.columns:
            raise ProviderError(f"stooq {ticker}: unexpected schema {list(frame.columns)}")

        frame["date"] = pd.to_datetime(frame["date"], utc=True)
        frame = frame.set_index("date")
        if "volume" not in frame.columns:
            frame["volume"] = 0.0
        frame = frame.loc[:, ["open", "high", "low", "close", "volume"]].astype(float)
        return ensure_utc_index(frame).tail(bars)
