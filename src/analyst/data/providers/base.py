"""Provider interface. Engines never touch this; only the repository does."""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from analyst.core.enums import Timeframe
from analyst.core.models import Instrument


class PriceProvider(ABC):
    """Fetches raw OHLCV. Implementations must return UTC-indexed frames.

    Contract:
      * columns exactly ["open", "high", "low", "close", "volume"]
      * tz-aware UTC DatetimeIndex, ascending, unique
      * may include a forming last candle — the caller drops it
      * raise `DataUnavailableError` for "nothing there", `ProviderError` for
        transport/schema failures. The distinction drives retry behaviour.
    """

    name: str = "base"
    #: Timeframes this provider can serve natively.
    native_timeframes: tuple[Timeframe, ...] = ()

    @abstractmethod
    def fetch(self, instrument: Instrument, timeframe: Timeframe, bars: int) -> pd.DataFrame:
        ...

    def supports(self, timeframe: Timeframe) -> bool:
        return timeframe in self.native_timeframes


class FundamentalsProvider(ABC):
    """Company financials, valuation, dividends and the next earnings date."""

    name: str = "base"

    @abstractmethod
    def fetch(self, instrument: Instrument) -> dict:
        ...
