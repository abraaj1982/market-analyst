"""Persistence schema.

Two design choices worth stating:

* **Candles are stored, not re-fetched.** Free providers cap how far back
  intraday history goes (60 days for 15m, 2 years for 1h). Accumulating locally
  is the only way to ever have a multi-year intraday history — and it is the
  only viable path for the Omani market, where no free API exists at all.

* **Every analysis is a full, self-contained record.** The JSON payload holds
  the complete engine breakdown, so months later a signal can be re-examined
  exactly as it was produced, under the config and code versions stamped on it.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from analyst.core.clock import now_utc


class Base(DeclarativeBase):
    pass


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "ts", name="uq_candle"),
        Index("ix_candle_lookup", "symbol", "timeframe", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(24), default="unknown")


class Analysis(Base):
    """One complete analysis run for one instrument."""

    __tablename__ = "analyses"
    __table_args__ = (Index("ix_analysis_lookup", "symbol", "as_of"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(128), default="")
    market: Mapped[str] = mapped_column(String(16), default="")
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    spot: Mapped[float] = mapped_column(Float, nullable=False)
    direction: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    grade: Mapped[str] = mapped_column(String(12), nullable=False)
    regime: Mapped[str] = mapped_column(String(24), default="")
    actionable: Mapped[bool] = mapped_column(Boolean, default=False)
    report_ar: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    config_version: Mapped[str] = mapped_column(String(32), default="")
    code_version: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    signal: Mapped[Signal | None] = relationship(back_populates="analysis", uselist=False)


class Signal(Base):
    """A tradeable plan derived from an actionable analysis.

    This table is the honesty mechanism of the whole system: every signal is
    written down with its exact levels *before* the outcome is known, and the
    outcome tracker fills in what actually happened. Live statistics come from
    here — never from a backtest.
    """

    __tablename__ = "signals"
    __table_args__ = (Index("ix_signal_open", "status", "symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("analyses.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    direction: Mapped[int] = mapped_column(Integer, nullable=False)
    grade: Mapped[str] = mapped_column(String(12), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    entry: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_1: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_2: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_reward: Mapped[float] = mapped_column(Float, default=0.0)

    #: open | tp1_hit | tp2_hit | stopped | expired
    status: Mapped[str] = mapped_column(String(16), default="open")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_favourable_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_adverse_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    bars_held: Mapped[int | None] = mapped_column(Integer, nullable=True)

    analysis: Mapped[Analysis] = relationship(back_populates="signal")


class AlertLog(Base):
    """What was actually delivered, used for cooldown and state-change logic."""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_lookup", "symbol", "sent_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), default="telegram")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    direction: Mapped[int] = mapped_column(Integer, default=0)
    grade: Mapped[str] = mapped_column(String(12), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[str] = mapped_column(Text, default="")


class KeyValue(Base):
    """Small TTL cache for slow-moving external data (macro series, COT)."""

    __tablename__ = "kv_cache"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
