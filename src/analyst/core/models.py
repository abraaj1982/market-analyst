"""Core domain models.

Design rule: everything that gets persisted or shown to the user is a pydantic
model (validated, serialisable). Heavy numeric payloads (candle frames) stay as
pandas DataFrames inside plain dataclasses and never cross the API boundary raw.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from analyst.core.enums import (
    AssetClass,
    Direction,
    EngineId,
    GateStatus,
    Grade,
    Market,
    Regime,
    Timeframe,
)

# --------------------------------------------------------------------------- #
# Instruments
# --------------------------------------------------------------------------- #


class Instrument(BaseModel):
    """A tradable symbol plus everything the pipeline needs to treat it correctly."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(description="Canonical internal symbol, e.g. XAUUSD")
    name_ar: str
    market: Market
    asset_class: AssetClass
    provider_symbol: str = Field(description="Symbol as the data provider expects it")
    currency: str = "USD"
    #: Timeframes this instrument can actually be analysed on, given its data source.
    supported_timeframes: tuple[Timeframe, ...] = (Timeframe.H1, Timeframe.H4, Timeframe.D1)
    #: Retail shorting allowed? MSX is long-only; a bearish signal there is an exit, not a short.
    shortable: bool = True
    #: Point value used when phrasing distances in the report.
    price_decimals: int = 2

    @property
    def is_equity(self) -> bool:
        return self.asset_class in (AssetClass.EQUITY, AssetClass.INDEX)


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #

#: Canonical OHLCV column names used everywhere downstream.
OHLCV = ("open", "high", "low", "close", "volume")


@dataclass(slots=True)
class Series:
    """A timeframe's candles for one instrument.

    Invariants enforced at construction:
      * index is a tz-aware UTC DatetimeIndex, sorted ascending, unique
      * columns are exactly OHLCV
      * the last row is a CLOSED candle (the caller must drop forming candles)
    """

    instrument: Instrument
    timeframe: Timeframe
    df: pd.DataFrame
    #: True when this series was resampled from a lower timeframe rather than fetched.
    derived: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.df.index, pd.DatetimeIndex):
            raise ValueError(f"{self.timeframe}: index must be a DatetimeIndex")
        if self.df.index.tz is None:
            raise ValueError(f"{self.timeframe}: index must be tz-aware (UTC)")
        missing = [c for c in OHLCV if c not in self.df.columns]
        if missing:
            raise ValueError(f"{self.timeframe}: missing columns {missing}")
        self.df = self.df.loc[:, list(OHLCV)].sort_index()
        self.df = self.df[~self.df.index.duplicated(keep="last")]

    def __len__(self) -> int:
        return len(self.df)

    @property
    def last_close(self) -> float:
        return float(self.df["close"].iloc[-1])

    @property
    def last_ts(self) -> datetime:
        return self.df.index[-1].to_pydatetime()

    def tail(self, n: int) -> pd.DataFrame:
        return self.df.tail(n)


@dataclass(slots=True)
class DataQualityReport:
    """Data trustworthiness, in [0, 1]. Feeds directly into the confidence math."""

    score: float
    issues: list[str] = field(default_factory=list)
    per_timeframe: dict[Timeframe, float] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        return self.score >= 0.5


@dataclass(slots=True)
class MacroSnapshot:
    """Free macro series (FRED) resolved once per run and shared by all engines."""

    values: dict[str, float] = field(default_factory=dict)
    changes: dict[str, float] = field(default_factory=dict)  # % change over lookback
    as_of: dict[str, datetime] = field(default_factory=dict)
    available: bool = False


@dataclass(slots=True)
class MarketContext:
    """Everything an engine is allowed to see. Engines never perform I/O."""

    instrument: Instrument
    as_of: datetime
    series: dict[Timeframe, Series]
    quality: DataQualityReport
    regime: Regime = Regime.RANGING
    macro: MacroSnapshot = field(default_factory=MacroSnapshot)
    #: Free-form provider extras: fundamentals dict, COT frame, calendar events...
    extras: dict[str, Any] = field(default_factory=dict)

    def get(self, tf: Timeframe) -> Series | None:
        return self.series.get(tf)

    def require(self, *tfs: Timeframe) -> bool:
        """True when every requested timeframe is present and non-trivially sized."""
        return all(tf in self.series and len(self.series[tf]) >= 60 for tf in tfs)

    @property
    def spot(self) -> float:
        for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1):
            if tf in self.series:
                return self.series[tf].last_close
        raise ValueError("MarketContext has no series")


# --------------------------------------------------------------------------- #
# Engine output
# --------------------------------------------------------------------------- #


class Evidence(BaseModel):
    """One human-readable, machine-attributable reason behind an engine's verdict.

    `contribution` is the signed amount this item pushed the engine score by, so a
    report can always answer "why?" with numbers rather than adjectives.
    """

    code: str
    label_ar: str
    detail_ar: str = ""
    direction: Direction = Direction.NEUTRAL
    contribution: float = 0.0

    @property
    def icon(self) -> str:
        return self.direction.emoji


class EngineResult(BaseModel):
    """Uniform contract every engine returns. Pure data — no side effects."""

    engine: EngineId
    direction: Direction
    #: How strongly this engine leans, independent of sign. [0, 1]
    strength: float = Field(ge=0.0, le=1.0)
    #: How much this engine trusts its own inputs (data adequacy). [0, 1]
    quality: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    notes_ar: list[str] = Field(default_factory=list)
    #: Set when the engine could not run at all (missing data, unsupported market).
    skipped_reason: str | None = None

    @property
    def signed(self) -> float:
        """direction * strength, the value the aggregator actually consumes."""
        return float(self.direction.value) * self.strength

    @property
    def active(self) -> bool:
        return self.skipped_reason is None and self.quality > 0.0

    @field_validator("strength", "quality")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v:  # NaN
            raise ValueError("must be a finite number")
        return v

    @classmethod
    def skipped(cls, engine: EngineId, reason: str) -> EngineResult:
        return cls(
            engine=engine,
            direction=Direction.NEUTRAL,
            strength=0.0,
            quality=0.0,
            skipped_reason=reason,
        )


# --------------------------------------------------------------------------- #
# Scoring output
# --------------------------------------------------------------------------- #


class EngineContribution(BaseModel):
    """Per-engine audit row: exactly how much this engine moved the final number."""

    engine: EngineId
    weight: float
    direction: Direction
    strength: float
    quality: float
    effective_weight: float
    contribution: float
    skipped_reason: str | None = None


class GateResult(BaseModel):
    gate: str
    label_ar: str
    status: GateStatus
    detail_ar: str = ""
    blocking: bool = True

    @property
    def icon(self) -> str:
        return {"passed": "✅", "failed": "❌", "not_evaluated": "➖"}[self.status.value]


class RiskPlan(BaseModel):
    """Concrete, checkable trade geometry. None when no valid plan exists."""

    entry: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None = None
    risk_reward: float
    stop_distance: float
    atr: float
    basis_ar: str
    position_size_hint: str | None = None


class ScoreBreakdown(BaseModel):
    """The full audit trail of the confidence computation."""

    raw_signed_score: float          # S  in [-1, 1]
    calibrated_consensus: float = 0.0  # K = L(|S|), in [0, 1]
    coherence: float                 # 1 - lambda * dispersion
    dispersion: float
    data_quality: float
    news_factor: float
    regime_fit: float
    confidence: float                # final, [0, 1]
    contributions: list[EngineContribution] = Field(default_factory=list)
    total_effective_weight: float = 0.0
    #: Weight that *could* have participated for this asset class and regime.
    available_weight: float = 0.0
    active_engines: int = 0

    @property
    def coverage_ratio(self) -> float:
        """Share of the available weight that actually voted. The honest measure
        of how much of the system was awake for this decision."""
        if self.available_weight <= 0:
            return 0.0
        return round(self.total_effective_weight / self.available_weight, 4)


class AnalysisResult(BaseModel):
    """One complete analysis of one instrument at one point in time."""

    model_config = ConfigDict(ser_json_timedelta="iso8601")

    symbol: str
    name_ar: str
    market: Market
    as_of: datetime
    spot: float
    direction: Direction
    confidence: float
    grade: Grade
    regime: Regime
    breakdown: ScoreBreakdown
    engines: list[EngineResult] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)
    risk: RiskPlan | None = None
    report_ar: str = ""
    data_quality_issues: list[str] = Field(default_factory=list)
    config_version: str = "0"
    code_version: str = "0"

    @property
    def blocking_failures(self) -> list[GateResult]:
        return [g for g in self.gates if g.blocking and g.status is not GateStatus.PASSED]

    @property
    def is_actionable(self) -> bool:
        return self.grade in (Grade.A_PLUS, Grade.A) and not self.blocking_failures
