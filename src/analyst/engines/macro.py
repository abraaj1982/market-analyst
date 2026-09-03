"""Macro / intermarket engine.

For gold this is arguably the most important engine in the system, and the
reason is a single number: the **10-year real yield** (DFII10). Gold pays no
coupon, so its opportunity cost is what an inflation-protected Treasury pays.
Falling real yields make holding gold cheaper; rising real yields make it
expensive. That relationship is more durable than any chart pattern.

The rest of the panel:
  * **Dollar index** — gold is priced in dollars; a weaker dollar mechanically
    supports the price.
  * **Breakeven inflation** — the demand side of the store-of-value case.
  * **VIX** — risk appetite; spikes drive haven flows.

For FX pairs the same series are read as dollar strength or weakness. For
equities the panel is read as a financial-conditions backdrop and carries far
less weight (set in `settings.yaml`).

Every component scores the *change*, not the level. A 4% real yield is not
bullish or bearish for gold on its own — the market has already priced it. What
moves price is the direction of travel.
"""
from __future__ import annotations

from analyst.core.config import Settings
from analyst.core.enums import AssetClass, EngineId, Market
from analyst.core.models import EngineResult, MarketContext
from analyst.engines.base import Engine, ScoreBuilder, scale


class MacroEngine(Engine):
    id = EngineId.MACRO

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def applies_to(self, ctx: MarketContext) -> tuple[bool, str]:
        if ctx.instrument.market is Market.MSX:
            return False, "المؤشرات الكلية الأمريكية لا تنطبق على السوق العماني مباشرة"
        if not ctx.macro.available:
            return False, "البيانات الكلية (FRED) غير متاحة حالياً"
        return True, ""

    def _run(self, ctx: MarketContext) -> EngineResult:
        asset = ctx.instrument.asset_class
        changes = ctx.macro.changes
        values = ctx.macro.values
        builder = ScoreBuilder()
        metrics: dict[str, float] = {}

        # How each macro move maps onto this asset class.
        #   +1 => a RISE in the series is bullish for the instrument
        if asset in (AssetClass.METAL,):
            mapping = {"DFII10": -1.0, "DTWEXBGS": -1.0, "T10YIE": +0.6, "VIXCLS": +0.4,
                       "DGS10": -0.4}
        elif asset is AssetClass.FX:
            # watchlist FX pairs are all quoted with USD as the *quote* currency
            # except USDJPY, which is handled by the inversion below
            usd_sign = -1.0 if ctx.instrument.symbol != "USDJPY" else +1.0
            mapping = {"DTWEXBGS": usd_sign, "DGS10": usd_sign * 0.7,
                       "DFII10": usd_sign * 0.5, "VIXCLS": -0.3}
        else:  # equities and indices
            mapping = {"DGS10": -0.6, "DFII10": -0.5, "VIXCLS": -0.8, "DTWEXBGS": -0.2}

        scales = {"DFII10": 0.35, "DGS10": 0.45, "T10YIE": 0.25,
                  "DTWEXBGS": 2.5, "VIXCLS": 40.0, "UNRATE": 0.4}

        for sid, sign in mapping.items():
            if sid not in changes:
                continue
            change = changes[sid]
            value = sign * scale(change, scales.get(sid, 1.0))
            direction_word = "ارتفاع" if change > 0 else "انخفاض"
            builder.add(
                f"macro_{sid.lower()}",
                _LABELS[sid],
                value,
                abs(sign),
                detail_ar=(
                    f"{direction_word} بمقدار {abs(change):.2f}{_UNITS[sid]} "
                    f"— المستوى الحالي {values.get(sid, float('nan')):.2f}"
                ),
            )
            metrics[f"{sid}_change"] = round(change, 4)
            metrics[f"{sid}_level"] = round(values.get(sid, 0.0), 4)

        if not builder.items and builder.weight_total == 0:
            return EngineResult.skipped(self.id, "لا توجد سلاسل كلية صالحة للقراءة")

        if asset is AssetClass.METAL and "DFII10" in changes:
            builder.note(
                "real_yield_primary", "العائد الحقيقي هو المحرك الأساسي للذهب",
                "تكلفة الفرصة البديلة لحيازة الذهب تُقاس بعائد السندات المحمية من التضخم",
            )

        covered = sum(1 for sid in mapping if sid in changes)
        quality = min(1.0, covered / max(1, len(mapping)))
        return builder.result(self.id, quality=quality, metrics=metrics)


_LABELS = {
    "DFII10": "العائد الحقيقي 10 سنوات",
    "DGS10": "عائد سندات الخزانة 10 سنوات",
    "T10YIE": "توقعات التضخم 10 سنوات",
    "DTWEXBGS": "مؤشر الدولار المرجّح تجارياً",
    "VIXCLS": "مؤشر التقلب VIX",
    "UNRATE": "معدل البطالة",
}

_UNITS = {
    "DFII10": " نقطة مئوية", "DGS10": " نقطة مئوية", "T10YIE": " نقطة مئوية",
    "DTWEXBGS": "%", "VIXCLS": "%", "UNRATE": " نقطة مئوية",
}
