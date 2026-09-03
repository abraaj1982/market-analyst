#!/usr/bin/env python
"""استيراد تاريخ شموع من ملف CSV إلى قاعدة البيانات.

الاستخدام الأساسي: بناء تاريخ للسوق العماني أو لأي رمز لا يوفّره مزوّد مجاني.

    python scripts/import_csv.py BKMB history.csv --timeframe 1d

الملف يجب أن يحوي أعمدة التاريخ والسعر. أسماء الأعمدة تُطابَق بمرونة
(date/time/تاريخ ، open/o/فتح ، …) ويمكن تجاوزها بـ --column.
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
            f"تعذّر التعرّف على الأعمدة: {missing}\n"
            f"الأعمدة الموجودة: {list(frame.columns)}\n"
            f"استخدم --column ts=اسم_العمود لتحديدها يدوياً."
        )
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description="استيراد شموع من CSV")
    parser.add_argument("symbol", help="الرمز كما هو في watchlist.yaml")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--timeframe", default="1d", choices=[t.value for t in Timeframe])
    parser.add_argument("--column", action="append", default=[],
                        help="تحديد عمود يدوياً، مثل: --column ts=Date")
    parser.add_argument("--dayfirst", action="store_true", help="التواريخ بصيغة يوم/شهر/سنة")
    args = parser.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"الملف غير موجود: {args.csv}")

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
        raise SystemExit("لم يتبقَ أي صف صالح بعد التنظيف — راجع صيغة الملف")

    init_db()
    repo = CandleRepository([SyntheticProvider()])  # provider unused for a pure write
    written = repo.store(args.symbol.upper(), Timeframe(args.timeframe), frame, source="csv")

    print(f"✅ استُورد {written} شمعة للرمز {args.symbol.upper()} على إطار {args.timeframe}")
    print(f"   المدى: {frame.index[0]:%Y-%m-%d} → {frame.index[-1]:%Y-%m-%d}")
    if before != len(frame):
        print(f"   ⚠️ تم تجاهل {before - len(frame)} صفاً غير صالح")


if __name__ == "__main__":
    main()
