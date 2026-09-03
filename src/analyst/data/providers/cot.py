"""CFTC Commitments of Traders — free, official, weekly, no API key.

Answers a question no chart can: *who* is positioned, and how extremely. The
engine reads non-commercial (speculator) net positioning and converts it to a
percentile of its own 3-year history — an extreme reading is a contrarian
warning, not a trend confirmation.

Data arrives on Friday for the prior Tuesday, so it is always stale by design.
That lag is real and is surfaced in the report rather than papered over.
"""
from __future__ import annotations

import logging

import httpx
import pandas as pd

from analyst.core.errors import DataUnavailableError, ProviderError

log = logging.getLogger(__name__)

#: CFTC public Socrata dataset: legacy futures-only report.
_ENDPOINT = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

#: Internal symbol -> CFTC contract market name.
CONTRACTS: dict[str, str] = {
    "XAUUSD": "GOLD - COMMODITY EXCHANGE INC.",
    "XAGUSD": "SILVER - COMMODITY EXCHANGE INC.",
    "EURUSD": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "GBPUSD": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
    "USDJPY": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
    "SPX": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
}

#: USDJPY is quoted inverse to the JPY futures contract: long JPY = short USDJPY.
INVERTED: frozenset[str] = frozenset({"USDJPY"})


class CotProvider:
    name = "cftc_cot"

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def fetch(self, symbol: str, weeks: int = 160) -> pd.DataFrame:
        contract = CONTRACTS.get(symbol)
        if not contract:
            raise DataUnavailableError(f"No COT contract mapped for {symbol}")

        params = {
            "$where": f"market_and_exchange_names='{contract}'",
            "$select": (
                "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
                "noncomm_positions_short_all,comm_positions_long_all,"
                "comm_positions_short_all,open_interest_all"
            ),
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": str(weeks),
        }
        try:
            resp = httpx.get(_ENDPOINT, params=params, timeout=self.timeout)
            resp.raise_for_status()
            rows = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"CFTC {symbol}: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"CFTC {symbol}: invalid response") from exc

        if not rows:
            raise DataUnavailableError(f"CFTC {symbol}: no records")

        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["report_date_as_yyyy_mm_dd"], utc=True)
        numeric = [
            "noncomm_positions_long_all", "noncomm_positions_short_all",
            "comm_positions_long_all", "comm_positions_short_all", "open_interest_all",
        ]
        for col in numeric:
            frame[col] = pd.to_numeric(frame.get(col), errors="coerce")

        frame = frame.dropna(subset=numeric).set_index("date").sort_index()
        frame["spec_net"] = (
            frame["noncomm_positions_long_all"] - frame["noncomm_positions_short_all"]
        )
        frame["comm_net"] = (
            frame["comm_positions_long_all"] - frame["comm_positions_short_all"]
        )
        if symbol in INVERTED:
            frame["spec_net"] *= -1
            frame["comm_net"] *= -1
        oi = frame["open_interest_all"].replace(0.0, pd.NA)
        frame["spec_net_pct_oi"] = (frame["spec_net"] / oi).astype(float)
        return frame[["spec_net", "comm_net", "open_interest_all", "spec_net_pct_oi"]]
