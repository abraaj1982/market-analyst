"""Domain enumerations. Every value here is part of the persisted contract."""
from __future__ import annotations

from enum import Enum


class Direction(int, Enum):
    """Directional bias. Integer-valued so it can be used directly in the math."""

    BEARISH = -1
    NEUTRAL = 0
    BULLISH = 1

    @classmethod
    def from_score(cls, score: float, deadband: float = 0.10) -> Direction:
        if score > deadband:
            return cls.BULLISH
        if score < -deadband:
            return cls.BEARISH
        return cls.NEUTRAL

    @property
    def label(self) -> str:
        return {-1: "Bearish", 0: "Neutral", 1: "Bullish"}[self.value]

    @property
    def emoji(self) -> str:
        return {-1: "🔻", 0: "⚪", 1: "🔺"}[self.value]


class Timeframe(str, Enum):
    """Supported timeframes. `minutes` drives resampling and staleness checks.

    M1 and M5 exist for chart viewing only -- fetched live on demand by the
    dashboard (see `_read_frame` in api/app.py), never stored by the
    scheduler and never listed in any instrument's `supported_timeframes`.
    The confidence-score engines never see them; that boundary is what keeps
    this an addition to the chart, not a second, higher-frequency analysis
    pipeline the earlier swing-timeframes decision explicitly ruled out.
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1wk"

    @property
    def minutes(self) -> int:
        return {
            "1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1wk": 10080,
        }[self.value]

    @property
    def pandas_rule(self) -> str:
        """Offset alias used when resampling a lower timeframe into this one."""
        return {
            "1m": "1min", "5m": "5min", "15m": "15min", "1h": "1h", "4h": "4h",
            "1d": "1D", "1wk": "1W-MON",
        }[self.value]

    @property
    def label(self) -> str:
        return {
            "1m": "1-minute",
            "5m": "5-minute",
            "15m": "15-minute",
            "1h": "1-hour",
            "4h": "4-hour",
            "1d": "Daily",
            "1wk": "Weekly",
        }[self.value]


class AssetClass(str, Enum):
    FX = "fx"
    METAL = "metal"
    EQUITY = "equity"
    INDEX = "index"
    CRYPTO = "crypto"


class Market(str, Enum):
    """A market defines session hours, calendar, and data capabilities."""

    GLOBAL_FX = "global_fx"
    US = "us"
    MSX = "msx"          # Muscat Stock Exchange (Oman)
    CRYPTO = "crypto"
    #: Companies with no price feed at all, analysed from manually supplied
    #: financials and news. See `manual.py`.
    MANUAL = "manual"


class Regime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    QUIET = "quiet"

    @property
    def label(self) -> str:
        return {
            "trending": "Trending",
            "ranging": "Ranging",
            "high_volatility": "High volatility",
            "quiet": "Quiet",
        }[self.value]


class Grade(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"
    NO_TRADE = "NO_TRADE"

    @property
    def label(self) -> str:
        return {
            "A+": "Excellent setup",
            "A": "Strong setup",
            "B": "Acceptable setup",
            "C": "Weak signal",
            "NO_TRADE": "No opportunity",
        }[self.value]


class GateStatus(str, Enum):
    """A gate that could not be evaluated is NOT a passing gate."""

    PASSED = "passed"
    FAILED = "failed"
    NOT_EVALUATED = "not_evaluated"


class EngineId(str, Enum):
    TREND = "trend"
    ICT_SMC = "ict_smc"
    CLASSIC_TA = "classic_ta"
    INDICATORS = "indicators"
    MACRO = "macro"
    COT = "cot"
    VOLUME_SEASONALITY = "volume_seasonality"
    FUNDAMENTALS = "fundamentals"
    NEWS = "news"
    #: Manual / local-company engines, used where no price feed exists at all.
    DIVIDENDS = "dividends"
    SENTIMENT = "sentiment"

    @property
    def label(self) -> str:
        return {
            "trend": "Multi-timeframe trend",
            "ict_smc": "ICT / liquidity & structure",
            "classic_ta": "Classical technical analysis",
            "indicators": "Technical indicators",
            "macro": "Macro & intermarket",
            "cot": "COT positioning",
            "volume_seasonality": "Volume & seasonality",
            "fundamentals": "Fundamentals",
            "news": "News & economic calendar",
            "dividends": "Dividend quality",
            "sentiment": "News sentiment",
        }[self.value]
