"""FRED macro series (St. Louis Fed) — free, official, no API key required.

The CSV graph endpoint serves any series without authentication, which keeps
the macro engine key-free. An API key is supported but never required.

Series chosen for gold and FX work specifically:

  DFII10   10-year TIPS yield — the *real* rate. Gold is a zero-yield asset, so
           its opportunity cost is the real yield, not the nominal one. This is
           the strongest single macro driver of gold and the reason the macro
           engine is weighted heavily for metals.
  DTWEXBGS Broad trade-weighted dollar index — the denominator gold is priced in.
  DGS10    Nominal 10-year yield.
  T10YIE   10-year breakeven inflation (nominal minus real).
  VIXCLS   Volatility index — risk appetite.
  CPIAUCSL Headline CPI level (monthly).
  UNRATE   Unemployment rate (monthly).
"""
from __future__ import annotations

import io
import logging

import httpx
import pandas as pd

from analyst.core.errors import ProviderError

log = logging.getLogger(__name__)

_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

#: series_id -> (human label in Arabic, "higher value is bullish for gold?" sign)
SERIES: dict[str, tuple[str, int]] = {
    "DFII10":   ("10-year real yield", -1),
    "DTWEXBGS": ("Trade-weighted dollar index", -1),
    "DGS10":    ("10-year Treasury yield", -1),
    "T10YIE":   ("10-year breakeven inflation", +1),
    "VIXCLS":   ("VIX volatility index", +1),
    "CPIAUCSL": ("Consumer price index", +1),
    "UNRATE":   ("Unemployment rate", +1),
}


class FredProvider:
    """Fetches a macro series as a daily-indexed float Series."""

    name = "fred"

    def __init__(self, timeout: float = 20.0, api_key: str | None = None) -> None:
        self.timeout = timeout
        self.api_key = api_key

    def fetch_series(self, series_id: str, observations: int = 800) -> pd.Series:
        params: dict[str, str] = {"id": series_id}
        if self.api_key:
            params["api_key"] = self.api_key
        try:
            resp = httpx.get(_CSV_URL, params=params, timeout=self.timeout, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"FRED {series_id}: {exc}") from exc

        frame = pd.read_csv(io.StringIO(resp.text))
        if frame.shape[1] < 2:
            raise ProviderError(f"FRED {series_id}: unexpected schema")
        date_col, value_col = frame.columns[0], frame.columns[1]
        frame[date_col] = pd.to_datetime(frame[date_col], utc=True, errors="coerce")
        # FRED marks missing observations with "."
        frame[value_col] = pd.to_numeric(frame[value_col], errors="coerce")
        series = (
            frame.dropna(subset=[date_col])
            .set_index(date_col)[value_col]
            .dropna()
            .astype(float)
            .tail(observations)
        )
        series.name = series_id
        return series

    def fetch_all(self, observations: int = 800) -> dict[str, pd.Series]:
        """Best-effort: one dead series must not take down the macro engine."""
        out: dict[str, pd.Series] = {}
        for sid in SERIES:
            try:
                out[sid] = self.fetch_series(sid, observations)
            except Exception as exc:
                log.warning("FRED %s unavailable: %s", sid, exc)
        return out
