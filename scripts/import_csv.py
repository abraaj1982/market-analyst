#!/usr/bin/env python
"""Import candle history from a CSV file into the database.

Primary use: building history for a market no free provider serves — Muscat
Stock Exchange, or any symbol you have a broker export for.

    python scripts/import_csv.py BKMB history.csv --timeframe 1d

Column names are matched loosely (date/time, open/o, close/last, …) in English
and Arabic, and any of them can be overridden with --column.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analyst.core.clock import ensure_utc_index  # noqa: E402
from analyst.core.enums import Timeframe  # noqa: E402
from analyst.data.providers.synthetic import SyntheticProvider  # noqa: E402
from analyst.data.repository import CandleRepository  # noqa: E402
from analyst.storage.db import init_db  # noqa: E402

ALIASES = {
    "ts": {"date", "datetime", "time", "timestamp", "تاريخ", "التاريخ"},
    "open": {"open", "o", "افتتاح", "فتح"},
    "high": {"high", "h", "اعلى", "أعلى"},
    "low": {"low", "l", "ادنى", "أدنى"},
    "close": {"close", "c", "last", "اغلاق", "إغلاق", "الاغلاق"},
    "volume": {"volume", "vol", "v", "حجم", "الكمية"},
}


def resolve_columns(frame: pd.DataFrame, overrides: dict[str, str]) -> dict[str, str]:
    lowered = {str(c).strip().lower(): c for c in frame.columns}
    mapping: dict[str, str] = {}
    for field, names in ALIASES.items():
        if field in overrides:
            mapping[field] = overrides[field]
            continue
        match = next((lowered[n] for n in names if n in lowered), None)
        if match is not None:
            mapping[field] = match

    missing = [f for f in ("ts", "open", "high", "low", "close") if f not in mapping]
    if missing:
        raise SystemExit(
            f"Could not identify these columns: {missing}\n"
            f"Columns present: {list(frame.columns)}\n"
            f"Name them explicitly, e.g. --column ts=Date"
        )
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description="Import candles from a CSV file")
    parser.add_argument("symbol", help="Symbol as it appears in watchlist.yaml")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--timeframe", default="1d", choices=[t.value for t in Timeframe])
    parser.add_argument("--column", action="append", default=[],
                        help="Name a column explicitly, e.g. --column ts=Date")
    parser.add_argument("--dayfirst", action="store_true", help="Dates are DD/MM/YYYY")
    args = parser.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"File not found: {args.csv}")

    overrides = dict(pair.split("=", 1) for pair in args.column)
    raw = pd.read_csv(args.csv)
    mapping = resolve_columns(raw, overrides)

    frame = pd.DataFrame({
        "ts": pd.to_datetime(raw[mapping["ts"]], utc=True, dayfirst=args.dayfirst,
                             errors="coerce"),
        **{f: pd.to_numeric(raw[mapping[f]], errors="coerce")
           for f in ("open", "high", "low", "close")},
        "volume": (pd.to_numeric(raw[mapping["volume"]], errors="coerce")
                   if "volume" in mapping else 0.0),
    })

    before = len(frame)
    frame = frame.dropna(subset=["ts", "open", "high", "low", "close"])
    frame["volume"] = frame["volume"].fillna(0.0)
    frame = ensure_utc_index(frame.set_index("ts"))

    if frame.empty:
        raise SystemExit("No valid rows survived cleaning — check the file format")

    init_db()
    # The provider argument is unused for a pure write, but the repository
    # requires one; the synthetic provider is the cheapest placeholder.
    repo = CandleRepository([SyntheticProvider()])
    written = repo.store(args.symbol.upper(), Timeframe(args.timeframe), frame, source="csv")

    print(f"Imported {written} candles for {args.symbol.upper()} on {args.timeframe}")
    print(f"  Range: {frame.index[0]:%Y-%m-%d} to {frame.index[-1]:%Y-%m-%d}")
    if before != len(frame):
        print(f"  Skipped {before - len(frame)} invalid row(s)")


if __name__ == "__main__":
    main()
