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
    def arabic(self) -> str:
        return {-1: "هابط", 0: "محايد", 1: "صاعد"}[self.value]

    @property
    def emoji(self) -> str:
        return {-1: "🔻", 0: "⚪", 1: "🔺"}[self.value]


class Timeframe(str, Enum):
    """Supported timeframes. `minutes` drives resampling and staleness checks."""

    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1wk"

    @property
    def minutes(self) -> int:
        return {"15m": 15, "1h": 60, "4h": 240, "1d": 1440, "1wk": 10080}[self.value]

    @property
    def pandas_rule(self) -> str:
        """Offset alias used when resampling a lower timeframe into this one."""
        return {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D", "1wk": "1W-MON"}[self.value]

    @property
    def arabic(self) -> str:
        return {
            "15m": "15 دقيقة",
            "1h": "ساعة",
            "4h": "4 ساعات",
            "1d": "يومي",
            "1wk": "أسبوعي",
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


class Regime(str, Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    HIGH_VOLATILITY = "high_volatility"
    QUIET = "quiet"

    @property
    def arabic(self) -> str:
        return {
            "trending": "سوق اتجاهي",
            "ranging": "سوق عرضي",
            "high_volatility": "تقلب مرتفع",
            "quiet": "سوق هادئ",
        }[self.value]


class Grade(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"
    NO_TRADE = "NO_TRADE"

    @property
    def arabic(self) -> str:
        return {
            "A+": "فرصة ممتازة",
            "A": "فرصة قوية",
            "B": "فرصة مقبولة",
            "C": "إشارة ضعيفة",
            "NO_TRADE": "لا توجد فرصة",
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

    @property
    def arabic(self) -> str:
        return {
            "trend": "محرك الاتجاه متعدد الفريمات",
            "ict_smc": "محرك ICT / السيولة والبنية",
            "classic_ta": "محرك التحليل الكلاسيكي",
            "indicators": "محرك المؤشرات الفنية",
            "macro": "محرك الكلي والترابط",
            "cot": "محرك تموضع المضاربين",
            "volume_seasonality": "محرك الحجم والموسمية",
            "fundamentals": "محرك التحليل الأساسي",
            "news": "محرك الأخبار والتقويم",
        }[self.value]
