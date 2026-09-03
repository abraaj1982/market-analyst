"""Builds the MacroSnapshot from FRED, with a TTL cache.

Macro series update at most daily (several are monthly), so hitting FRED on
every 30-minute analysis run would be pure waste. The snapshot is cached in the
database and refreshed once every `ttl_hours`.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from analyst.core.clock import now_utc, to_utc
from analyst.core.models import MacroSnapshot
from analyst.data.providers.fred import SERIES, FredProvider
from analyst.storage.db import session_scope
from analyst.storage.models import KeyValue

log = logging.getLogger(__name__)

_CACHE_KEY = "macro_snapshot"

#: Lookback in observations for the "change" figure of each series.
_CHANGE_LOOKBACK = 20


class MacroLoader:
    def __init__(self, provider: FredProvider | None = None, ttl_hours: int = 6) -> None:
        self.provider = provider or FredProvider()
        self.ttl = timedelta(hours=ttl_hours)

    def __call__(self) -> MacroSnapshot:
        cached = self._read_cache()
        if cached is not None:
            return cached
        snapshot = self._fetch()
        self._write_cache(snapshot)
        return snapshot

    # ------------------------------------------------------------------ #

    def _fetch(self) -> MacroSnapshot:
        series_map = self.provider.fetch_all(observations=400)
        values: dict[str, float] = {}
        changes: dict[str, float] = {}
        as_of: dict = {}

        for sid, series in series_map.items():
            if series.empty:
                continue
            values[sid] = float(series.iloc[-1])
            as_of[sid] = to_utc(series.index[-1])
            if len(series) > _CHANGE_LOOKBACK:
                past = float(series.iloc[-_CHANGE_LOOKBACK])
                # absolute change for rates (already in %), relative for levels
                if sid in {"DFII10", "DGS10", "T10YIE", "UNRATE"}:
                    changes[sid] = values[sid] - past
                elif past != 0:
                    changes[sid] = (values[sid] / past - 1.0) * 100.0

        return MacroSnapshot(
            values=values, changes=changes, as_of=as_of, available=bool(values)
        )

    def _read_cache(self) -> MacroSnapshot | None:
        with session_scope() as session:
            row = session.execute(
                select(KeyValue).where(KeyValue.key == _CACHE_KEY)
            ).scalar_one_or_none()
            if row is None:
                return None
            if now_utc() - to_utc(row.updated_at) > self.ttl:
                return None
            payload = dict(row.value or {})
        return MacroSnapshot(
            values=payload.get("values", {}),
            changes=payload.get("changes", {}),
            as_of={},
            available=bool(payload.get("values")),
        )

    def _write_cache(self, snapshot: MacroSnapshot) -> None:
        payload = {"values": snapshot.values, "changes": snapshot.changes}
        with session_scope() as session:
            row = session.get(KeyValue, _CACHE_KEY)
            if row is None:
                session.add(KeyValue(key=_CACHE_KEY, value=payload, updated_at=now_utc()))
            else:
                row.value = payload
                row.updated_at = now_utc()


def label_for(series_id: str) -> str:
    return SERIES.get(series_id, (series_id, 0))[0]
