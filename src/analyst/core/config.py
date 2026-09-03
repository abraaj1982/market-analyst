"""Configuration loading and validation.

Contract:
  * YAML files under `config/` are the source of truth for behaviour.
  * `.env` / environment variables hold ONLY secrets and machine-specific paths.
  * Anything invalid fails loudly at startup, never silently at 3am.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from analyst.core.enums import AssetClass, Market, Timeframe
from analyst.core.errors import ConfigError
from analyst.core.models import Instrument

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

PROJECT_ROOT = Path(os.getenv("ANALYST_ROOT", Path(__file__).resolve().parents[3]))
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
WEB_DIR = PROJECT_ROOT / "web"


# --------------------------------------------------------------------------- #
# Settings schema
# --------------------------------------------------------------------------- #


class TimezoneSettings(BaseModel):
    storage: str = "UTC"
    display: str = "Asia/Muscat"


class ProfileSettings(BaseModel):
    timeframes: list[Timeframe]
    mtf_weights: dict[Timeframe, float]
    analysis_interval_minutes: int = 30

    @model_validator(mode="after")
    def _weights_cover_timeframes(self) -> ProfileSettings:
        missing = [tf for tf in self.timeframes if tf not in self.mtf_weights]
        if missing:
            raise ValueError(f"mtf_weights missing entries for {missing}")
        total = sum(self.mtf_weights.values())
        if total <= 0:
            raise ValueError("mtf_weights must sum to a positive number")
        return self

    @property
    def normalised_mtf_weights(self) -> dict[Timeframe, float]:
        total = sum(self.mtf_weights[tf] for tf in self.timeframes)
        return {tf: self.mtf_weights[tf] / total for tf in self.timeframes}


class WeightSettings(BaseModel):
    base: dict[str, float]
    regime_multipliers: dict[str, dict[str, float]] = Field(default_factory=dict)
    asset_class_multipliers: dict[str, dict[str, float]] = Field(default_factory=dict)


class GradeThresholds(BaseModel):
    A_PLUS: float = 0.80
    A: float = 0.70
    B: float = 0.60
    C: float = 0.45


class CalibrationSettings(BaseModel):
    """Platt-style mapping from raw consensus to a usable confidence scale."""

    midpoint: float = 0.33
    steepness: float = 9.5


class ScoringSettings(BaseModel):
    direction_deadband: float = 0.10
    dispersion_lambda: float = 0.55
    min_coverage_ratio: float = 0.50
    min_active_engines: int = 3
    news_penalty_high: float = 0.35
    news_penalty_medium: float = 0.75
    calibration: CalibrationSettings = Field(default_factory=CalibrationSettings)
    regime_fit: dict[str, float] = Field(
        default_factory=lambda: {
            "trending": 1.0, "ranging": 0.92, "quiet": 0.88, "high_volatility": 0.82
        }
    )
    grades: GradeThresholds = Field(default_factory=GradeThresholds)

    @model_validator(mode="after")
    def _grades_ordered(self) -> ScoringSettings:
        g = self.grades
        if not (g.A_PLUS > g.A > g.B > g.C):
            raise ValueError("grade thresholds must be strictly descending")
        return self


class RiskSettings(BaseModel):
    atr_period: int = 14
    atr_stop_multiplier: float = 1.6
    min_risk_reward: float = 1.8
    target_r_multiple_1: float = 2.0
    target_r_multiple_2: float = 3.5
    account_risk_percent: float = 1.0
    structure_stop_buffer_atr: float = 0.35
    max_stop_atr: float = 3.0
    max_level_distance_atr: float = 10.0


class DataQualitySettings(BaseModel):
    max_staleness_bars: int = 3
    max_gap_ratio: float = 0.08
    min_bars_required: int = 120
    spike_atr_multiple: float = 8.0


class AlertSettings(BaseModel):
    enabled: bool = True
    min_grade: str = "A"
    cooldown_minutes: int = 240
    require_state_change: bool = True
    daily_digest_hour_local: int = 7


class StorageSettings(BaseModel):
    database_url: str = "sqlite:///data/analyst.db"
    candle_retention_days: int = 1825


class Settings(BaseModel):
    version: str = "1.0.0"
    profile: str = "swing"
    timezone: TimezoneSettings = Field(default_factory=TimezoneSettings)
    profiles: dict[str, ProfileSettings]
    weights: WeightSettings
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    data_quality: DataQualitySettings = Field(default_factory=DataQualitySettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)

    @model_validator(mode="after")
    def _active_profile_exists(self) -> Settings:
        if self.profile not in self.profiles:
            raise ValueError(f"profile '{self.profile}' is not defined under profiles:")
        return self

    @property
    def active_profile(self) -> ProfileSettings:
        return self.profiles[self.profile]

    def resolved_db_url(self) -> str:
        """Turn a relative sqlite path into an absolute one rooted at the project."""
        url = os.getenv("ANALYST_DATABASE_URL", self.storage.database_url)
        prefix = "sqlite:///"
        if url.startswith(prefix) and not url.startswith(prefix + "/"):
            rel = url[len(prefix):]
            return f"{prefix}{(PROJECT_ROOT / rel).as_posix()}"
        return url


class GateSpec(BaseModel):
    id: str
    label: str
    blocking: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Secrets (environment only — never committed)
# --------------------------------------------------------------------------- #


class Secrets(BaseModel):
    """Optional credentials. The system runs fully without any of them."""

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    fred_api_key: str | None = None

    @classmethod
    def from_env(cls) -> Secrets:
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
            fred_api_key=os.getenv("FRED_API_KEY") or None,
        )

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


# --------------------------------------------------------------------------- #
# Loaders
# --------------------------------------------------------------------------- #


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed YAML in {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must have a mapping at its root")
    return data


@lru_cache(maxsize=1)
def load_settings(path: Path | None = None) -> Settings:
    src = path or CONFIG_DIR / "settings.yaml"
    try:
        return Settings.model_validate(_read_yaml(src))
    except ValidationError as exc:
        raise ConfigError(f"Invalid settings in {src.name}:\n{exc}") from exc


@lru_cache(maxsize=1)
def load_gates(path: Path | None = None) -> list[GateSpec]:
    src = path or CONFIG_DIR / "gates.yaml"
    raw = _read_yaml(src).get("gates", [])
    try:
        return [GateSpec.model_validate(g) for g in raw]
    except ValidationError as exc:
        raise ConfigError(f"Invalid gates in {src.name}:\n{exc}") from exc


#: Default timeframe capability per market, used when the watchlist omits it.
_MARKET_DEFAULT_TFS: dict[Market, tuple[Timeframe, ...]] = {
    Market.GLOBAL_FX: (Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1),
    Market.US: (Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1),
    Market.CRYPTO: (Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1),
    Market.MSX: (Timeframe.D1, Timeframe.W1),
}


class WatchlistEntry(BaseModel):
    """Raw watchlist row before it becomes an `Instrument`."""

    symbol: str
    name: str
    market: Market
    asset_class: AssetClass
    provider_symbol: str
    fallback_symbols: list[str] = Field(default_factory=list)
    currency: str = "USD"
    supported_timeframes: list[Timeframe] | None = None
    shortable: bool | None = None
    price_decimals: int = 2
    enabled: bool = True

    def to_instrument(self) -> Instrument:
        tfs = tuple(self.supported_timeframes or _MARKET_DEFAULT_TFS[self.market])
        shortable = self.shortable if self.shortable is not None else self.market is not Market.MSX
        return Instrument(
            symbol=self.symbol,
            name=self.name,
            market=self.market,
            asset_class=self.asset_class,
            provider_symbol=self.provider_symbol,
            currency=self.currency,
            supported_timeframes=tfs,
            shortable=shortable,
            price_decimals=self.price_decimals,
        )


@lru_cache(maxsize=1)
def load_watchlist(path: Path | None = None) -> list[WatchlistEntry]:
    src = path or CONFIG_DIR / "watchlist.yaml"
    raw = _read_yaml(src).get("instruments", [])
    try:
        entries = [WatchlistEntry.model_validate(e) for e in raw]
    except ValidationError as exc:
        raise ConfigError(f"Invalid watchlist in {src.name}:\n{exc}") from exc
    seen: set[str] = set()
    for e in entries:
        if e.symbol in seen:
            raise ConfigError(f"Duplicate symbol in the watchlist: {e.symbol}")
        seen.add(e.symbol)
    return entries


def active_instruments() -> list[Instrument]:
    return [e.to_instrument() for e in load_watchlist() if e.enabled]


def fallbacks_for(symbol: str) -> list[str]:
    for e in load_watchlist():
        if e.symbol == symbol:
            return list(e.fallback_symbols)
    return []


def reset_caches() -> None:
    """Used by tests and by the `reload` CLI command."""
    load_settings.cache_clear()
    load_gates.cache_clear()
    load_watchlist.cache_clear()
