#!/usr/bin/env python
"""Bulk-load the manual company register from a CSV file.

Primary use: seeding Muscat Stock Exchange (MSX) companies, or any market
with no free price feed, without typing one `analyst company add` per row.

    python scripts/import_companies.py seeds/msx_companies.csv

Expected columns: symbol,name,sector,currency (currency optional, defaults
to OMR). Any other manual-register field (price, dividend, eps, ...) is
accepted too if the column is present -- see `upsert_company` for the full
field list.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analyst.storage.db import init_db  # noqa: E402

NUMERIC_FIELDS = {
    "price", "dividend_per_share", "previous_dividend_per_share", "eps",
    "book_value_per_share", "debt_to_equity",
}
INT_FIELDS = {"dividend_years_paid", "dividend_years_cut"}
ALL_FIELDS = {"name", "sector", "currency"} | NUMERIC_FIELDS | INT_FIELDS


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-import companies into the manual register")
    parser.add_argument("csv", type=Path)
    args = parser.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"File not found: {args.csv}")

    frame = pd.read_csv(args.csv, dtype=str).fillna("")
    if "symbol" not in frame.columns or "name" not in frame.columns:
        raise SystemExit("CSV must have at least 'symbol' and 'name' columns")

    init_db()
    from analyst.manual.service import upsert_company

    added = 0
    for _, row in frame.iterrows():
        symbol = row["symbol"].strip().upper()
        if not symbol:
            continue
        fields: dict[str, object] = {"currency": "OMR"}
        for col in frame.columns:
            if col == "symbol" or col not in ALL_FIELDS:
                continue
            value = row[col].strip()
            if not value:
                continue
            if col in NUMERIC_FIELDS:
                fields[col] = float(value)
            elif col in INT_FIELDS:
                fields[col] = int(value)
            else:
                fields[col] = value
        upsert_company(symbol, fields)
        added += 1

    print(f"Loaded {added} companies into the manual register")


if __name__ == "__main__":
    main()
