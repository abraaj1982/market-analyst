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
    name: Mapped[str] = mapped_column(String(128), default="")
    market: Mapped[str] = mapped_column(String(16), default="")
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    spot: Mapped[float] = mapped_column(Float, nullable=False)
    direction: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    grade: Mapped[str] = mapped_column(String(12), nullable=False)
    regime: Mapped[str] = mapped_column(String(24), default="")
    actionable: Mapped[bool] = mapped_column(Boolean, default=False)
    report: Mapped[str] = mapped_column(Text, default="")
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


class ManualCompany(Base):
    """A company with no price feed, analysed from supplied figures and news.

    This exists because Muscat Stock Exchange (and similar local markets) have
    no free price API. Everything here is entered by a person, so the schema is
    deliberately permissive: every financial field is optional, and the engines
    reduce their own confidence in proportion to what is missing rather than
    refusing to run or, worse, assuming a value.
    """

    __tablename__ = "manual_companies"
    __table_args__ = (UniqueConstraint("symbol", name="uq_manual_company_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    market: Mapped[str] = mapped_column(String(32), default="msx")
    sector: Mapped[str] = mapped_column(String(80), default="")
    currency: Mapped[str] = mapped_column(String(8), default="OMR")
    notes: Mapped[str] = mapped_column(Text, default="")

    #: Latest price the user entered. Optional: without it, yield cannot be
    #: computed and the dividend engine says so instead of guessing.
    price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- dividend inputs -------------------------------------------------
    dividend_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    dividend_years_paid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dividend_years_cut: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_dividend_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- earnings and balance sheet --------------------------------------
    eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    book_value_per_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_growth: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    news: Mapped[list[CompanyNews]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class CompanyNews(Base):
    """One announcement or headline about a manual company.

    Sentiment is scored by a transparent keyword lexicon, and the matched terms
    are stored alongside the score. That is the whole point: the reader can see
    exactly which words produced the reading and disagree with it.
    """

    __tablename__ = "company_news"
    __table_args__ = (Index("ix_company_news_lookup", "company_id", "published_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("manual_companies.id", ondelete="CASCADE"), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(120), default="")

    #: Lexicon score in [-1, 1], and the terms that produced it.
    sentiment: Mapped[float] = mapped_column(Float, default=0.0)
    matched_terms: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Set when a person overrides the automatic reading.
    manual_sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    company: Mapped[ManualCompany] = relationship(back_populates="news")

    @property
    def effective_sentiment(self) -> float:
        return self.manual_sentiment if self.manual_sentiment is not None else self.sentiment
