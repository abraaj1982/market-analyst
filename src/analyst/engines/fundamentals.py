"""Fundamental engine for equities.

Four pillars, each scored against sector-agnostic reference bands and combined
into one verdict. This engine answers a different question from every other one
in the system — "is this a good business at a reasonable price?" — which is why
it is weighted heavily for equities and zeroed out entirely for FX and metals.

  * **Valuation** — trailing/forward P/E, P/B, PEG. Cheap is bullish, but a P/E
    of 4 usually means the market expects earnings to collapse, so the extreme
    low end is not rewarded further.
  * **Profitability** — margins and return on equity.
  * **Growth** — revenue and earnings growth.
  * **Balance sheet** — leverage and liquidity. This one can only ever *hurt*
    the score: a strong balance sheet is table stakes, a weak one is a risk.

Dividend yield is scored separately and deliberately matters more for the Omani
market, where total return is dividend-dominated.
"""
from __future__ import annotations

from analyst.core.config import Settings
from analyst.core.enums import EngineId, Market
from analyst.core.models import EngineResult, MarketContext
from analyst.engines.base import Engine, ScoreBuilder, clamp


def _band(value: float | None, good: float, bad: float) -> float | None:
    """Map a metric onto [-1, 1] where `good` scores +1 and `bad` scores -1.

    Works in both directions: `good` may be below `bad` (P/E, leverage) or above
    it (margin, growth). Values beyond either end saturate rather than extending
    the scale, so one spectacular metric cannot carry the whole verdict.
    """
    if value is None:
        return None
    if good == bad:
        return 0.0
    return clamp(2.0 * (bad - value) / (bad - good) - 1.0)


class FundamentalsEngine(Engine):
    id = EngineId.FUNDAMENTALS

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def applies_to(self, ctx: MarketContext) -> tuple[bool, str]:
        if not ctx.instrument.is_equity:
            return False, "التحليل الأساسي لا ينطبق على العملات والمعادن"
        if not ctx.extras.get("fundamentals"):
            return False, "بيانات الأساسيات غير متاحة لهذا الرمز"
        return True, ""

    def _run(self, ctx: MarketContext) -> EngineResult:
        f = ctx.extras["fundamentals"]
        builder = ScoreBuilder()
        metrics: dict[str, float] = {}
        notes: list[str] = []

        # --- valuation --------------------------------------------------
        pe = f.get("forward_pe") or f.get("trailing_pe")
        pe_score = _band(pe, good=12.0, bad=45.0)
        if pe_score is not None:
            if pe is not None and pe < 6.0:
                pe_score = min(pe_score, 0.2)  # suspiciously cheap is not a bargain
                notes.append(f"مكرر ربحية منخفض جداً ({pe:.1f}) — قد يعكس توقعات بتدهور الأرباح")
            builder.add("valuation_pe", "مكرر الربحية", pe_score, 1.1,
                        detail_ar=f"P/E = {pe:.1f}")
            metrics["pe"] = round(float(pe), 2)

        pb_score = _band(f.get("price_to_book"), good=1.2, bad=8.0)
        if pb_score is not None:
            builder.add("valuation_pb", "مكرر القيمة الدفترية", pb_score, 0.7,
                        detail_ar=f"P/B = {f['price_to_book']:.2f}")
            metrics["pb"] = round(float(f["price_to_book"]), 2)

        peg_score = _band(f.get("peg_ratio"), good=0.8, bad=3.0)
        if peg_score is not None:
            builder.add("valuation_peg", "مكرر الربحية إلى النمو (PEG)", peg_score, 0.8,
                        detail_ar=f"PEG = {f['peg_ratio']:.2f}")

        # --- profitability ---------------------------------------------
        margin_score = _band(f.get("profit_margin"), good=0.25, bad=0.02)
        if margin_score is not None:
            builder.add("profit_margin", "هامش الربح الصافي", margin_score, 1.0,
                        detail_ar=f"هامش الربح {f['profit_margin']:.1%}")
            metrics["profit_margin"] = round(float(f["profit_margin"]), 4)

        roe_score = _band(f.get("return_on_equity"), good=0.20, bad=0.03)
        if roe_score is not None:
            builder.add("return_on_equity", "العائد على حقوق الملكية", roe_score, 1.0,
                        detail_ar=f"ROE = {f['return_on_equity']:.1%}")

        # --- growth -----------------------------------------------------
        rev_score = _band(f.get("revenue_growth"), good=0.20, bad=-0.05)
        if rev_score is not None:
            builder.add("revenue_growth", "نمو الإيرادات", rev_score, 1.1,
                        detail_ar=f"نمو الإيرادات {f['revenue_growth']:+.1%}")
            metrics["revenue_growth"] = round(float(f["revenue_growth"]), 4)

        eps_score = _band(f.get("earnings_growth"), good=0.20, bad=-0.10)
        if eps_score is not None:
            builder.add("earnings_growth", "نمو الأرباح", eps_score, 1.1,
                        detail_ar=f"نمو الأرباح {f['earnings_growth']:+.1%}")

        # --- balance sheet (penalty only) -------------------------------
        de = f.get("debt_to_equity")
        if de is not None:
            penalty = -clamp((de - 100.0) / 150.0)
            if penalty < -0.05:
                builder.add("leverage", "الرفع المالي", penalty, 1.0,
                            detail_ar=f"الدين إلى حقوق الملكية {de:.0f}% — مستوى مرتفع")
            metrics["debt_to_equity"] = round(float(de), 1)

        cr = f.get("current_ratio")
        if cr is not None and cr < 1.0:
            builder.add("liquidity", "السيولة قصيرة الأجل", -clamp((1.0 - cr) * 1.5), 0.8,
                        detail_ar=f"نسبة التداول {cr:.2f} — أقل من 1")

        # --- dividends ---------------------------------------------------
        dy = f.get("dividend_yield")
        if dy is not None and dy > 0:
            # dividends dominate total return in the Omani market
            weight = 1.2 if ctx.instrument.market is Market.MSX else 0.6
            payout = f.get("payout_ratio")
            score = clamp(dy / 0.06)
            if payout is not None and payout > 0.9:
                score *= 0.4
                notes.append(f"نسبة التوزيع {payout:.0%} مرتفعة — استدامة التوزيع محل شك")
            builder.add("dividend_yield", "عائد التوزيعات", score, weight,
                        detail_ar=f"عائد التوزيعات {dy:.2%}")
            metrics["dividend_yield"] = round(float(dy), 4)

        if builder.weight_total == 0:
            return EngineResult.skipped(self.id, "لا توجد مؤشرات أساسية صالحة")

        # quality reflects how much of the intended panel actually had data
        expected_weight = 9.4
        quality = clamp(builder.weight_total / expected_weight, 0.0, 1.0)
        return builder.result(self.id, quality=quality, metrics=metrics, notes_ar=notes)
