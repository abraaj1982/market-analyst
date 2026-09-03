"""Candle repository: the only component that talks to providers and the DB.

Read path is cache-first: fetch from the provider, merge into the local store,
then read the merged history back out. That merge is what lets a two-year
provider window grow into a multi-year local history over time, and it is the
only reason a market with no historical API (MSX) can ever be analysed at all.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from analyst.core.clock import ensure_utc_index, now_utc, to_utc
from analyst.core.enums import Timeframe
from analyst.core.errors import DataUnavailableError
from analyst.core.models import Instrument
from analyst.data.providers.base import PriceProvider
from analyst.storage.db import session_scope
from analyst.storage.models import Candle

log = logging.getLogger(__name__)


class CandleRepository:
    def __init__(self, providers: list[PriceProvider]) -> None:
        if not providers:
            raise ValueError("CandleRepository requires at least one provider")
        self.providers = providers

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def load(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        bars: int,
        refresh: bool = True,
    ) -> pd.DataFrame:
        """Refreshed, merged history for one instrument/timeframe."""
        if refresh:
            try:
                fetched = self._fetch(instrument, timeframe, bars)
                if not fetched.empty:
                    self.store(instrument.symbol, timeframe, fetched, self._last_source)
            except DataUnavailableError as exc:
                log.warning("%s", exc)  # fall through to whatever is cached

        cached = self.read(instrument.symbol, timeframe, bars)
        if cached.empty:
            raise DataUnavailableError(
                f"{instrument.symbol} @ {timeframe.value}: no live data and nothing cached"
            )
        return cached

    def store(
        self, symbol: str, timeframe: Timeframe, frame: pd.DataFrame, source: str = "unknown"
    ) -> int:
        """Idempotent upsert. Re-running the same fetch writes nothing new."""
        if frame.empty:
            return 0
        frame = ensure_utc_index(frame)
        rows = [
            {
                "symbol": symbol,
                "timeframe": timeframe.value,
                "ts": to_utc(ts),
                "open": float(r.open), "high": float(r.high),
                "low": float(r.low), "close": float(r.close),
                "volume": float(r.volume) if pd.notna(r.volume) else 0.0,
                "source": source,
            }
            for ts, r in frame.iterrows()
        ]
        with session_scope() as session:
            stmt = sqlite_insert(Candle).values(rows)
            # a later fetch may correct an earlier provisional bar
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "timeframe", "ts"],
                set_={
                    "open": stmt.excluded.open, "high": stmt.excluded.high,
                    "low": stmt.excluded.low, "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume, "source": stmt.excluded.source,
                },
            )
            session.execute(stmt)
        return len(rows)

    def read(self, symbol: str, timeframe: Timeframe, bars: int) -> pd.DataFrame:
        with session_scope() as session:
            stmt = (
                select(Candle.ts, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume)
                .where(Candle.symbol == symbol, Candle.timeframe == timeframe.value)
                .order_by(Candle.ts.desc())
                .limit(bars)
            )
            rows = session.execute(stmt).all()
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        frame = frame.set_index("ts").sort_index()
        return ensure_utc_index(frame)

    def prune(self, retention_days: int) -> int:
        """Drop candles older than the retention window (intraday only).

        Daily and weekly bars are cheap and irreplaceable, so they are kept
        forever; only intraday frames are trimmed.
        """
        cutoff = now_utc() - timedelta(days=retention_days)
        intraday = [tf.value for tf in (Timeframe.M15, Timeframe.H1, Timeframe.H4)]
        with session_scope() as session:
            result = session.execute(
                delete(Candle).where(Candle.ts < cutoff, Candle.timeframe.in_(intraday))
            )
        return int(result.rowcount or 0)

    def coverage(self, symbol: str, timeframe: Timeframe) -> tuple[int, pd.Timestamp | None]:
        """(row count, newest timestamp) — used by the CLI status command."""
        with session_scope() as session:
            rows = session.execute(
                select(Candle.ts)
                .where(Candle.symbol == symbol, Candle.timeframe == timeframe.value)
                .order_by(Candle.ts.desc())
            ).scalars().all()
        return len(rows), (pd.Timestamp(rows[0]) if rows else None)

    # ------------------------------------------------------------------ #

    _last_source: str = "unknown"

    def _fetch(self, instrument: Instrument, timeframe: Timeframe, bars: int) -> pd.DataFrame:
        """Try providers in order; the first non-empty result wins."""
        errors: list[str] = []
        for provider in self.providers:
            if not provider.supports(timeframe):
                continue
            try:
                frame = provider.fetch(instrument, timeframe, bars)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if not frame.empty:
                self._last_source = provider.name
                return frame
            errors.append(f"{provider.name}: empty")

        raise DataUnavailableError(
            f"{instrument.symbol} @ {timeframe.value}: every provider failed "
            f"({'; '.join(errors) or 'no provider supports this timeframe'})"
        )
