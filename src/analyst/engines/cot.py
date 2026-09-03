"""COT positioning engine (CFTC Commitments of Traders).

Reads two things from weekly speculator positioning, and they pull in opposite
directions on purpose:

  * **Trend of positioning** — specs adding to longs alongside a rising price is
    confirmation that the move has participation behind it.
  * **Extremity of positioning** — but when net length reaches the top decile of
    its own three-year range, the marginal buyer is running out. That is a
    contrarian warning, and it *overrides* the trend read at the extremes.

Two honesty constraints are built in:

  * The report is published Friday for the *prior Tuesday*, so it is between 3
    and 9 days stale. The staleness is measured and reduces the engine's own
    quality score rather than being ignored.
  * Positioning is a *conditioning* signal, not a timing one. Extremes can
    persist for months, which is why this engine carries a low base weight.
"""
from __future__ import annotations

import pandas as pd

from analyst.core.config import Settings
from analyst.core.enums import AssetClass, EngineId
from analyst.core.models import EngineResult, MarketContext
from analyst.engines.base import Engine, ScoreBuilder, clamp, scale


class CotEngine(Engine):
    id = EngineId.COT

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def applies_to(self, ctx: MarketContext) -> tuple[bool, str]:
        if ctx.instrument.asset_class not in (AssetClass.FX, AssetClass.METAL, AssetClass.INDEX):
            return False, "تقرير COT لا يغطي هذه الفئة من الأصول"
        frame = ctx.extras.get("cot")
        if frame is None or len(frame) < 26:
            return False, "بيانات COT غير متاحة أو قصيرة جداً (أقل من 26 أسبوعاً)"
        return True, ""

    def _run(self, ctx: MarketContext) -> EngineResult:
        frame: pd.DataFrame = ctx.extras["cot"]
        builder = ScoreBuilder()
        metrics: dict[str, float] = {}

        net = frame["spec_net"].dropna()
        latest = float(net.iloc[-1])
        history = net.tail(156)  # ~3 years of weekly observations
        rank = float((history <= latest).sum() - 1) / max(1, len(history) - 1)
        metrics["spec_net"] = latest
        metrics["spec_net_percentile"] = round(rank, 4)

        # --- trend of positioning -------------------------------------
        change_4w = latest - float(net.iloc[-5]) if len(net) >= 5 else 0.0
        denom = float(net.tail(52).abs().mean()) or 1.0
        builder.add(
            "cot_trend", "اتجاه تموضع المضاربين",
            scale(change_4w / denom, 0.35), 1.0,
            detail_ar=(
                f"صافي مراكز المضاربين تغيّر بمقدار {change_4w:+,.0f} عقد خلال 4 أسابيع "
                f"(الصافي الحالي {latest:+,.0f})"
            ),
        )
        metrics["spec_net_change_4w"] = round(change_4w, 2)

        # --- extremity (contrarian) ------------------------------------
        if rank >= 0.90 or rank <= 0.10:
            # far from neutral: fade. Weight rises the further into the tail.
            extremity = (rank - 0.5) * 2.0
            builder.add(
                "cot_extreme", "تموضع متطرف — إشارة معاكسة",
                -clamp(extremity) * 0.9, 1.6,
                detail_ar=(
                    f"الصافي عند المئين {rank:.0%} من آخر 3 سنوات — "
                    f"{'ازدحام في الشراء' if rank >= 0.9 else 'ازدحام في البيع'}، "
                    "المشتري/البائع الحدّي ينفد"
                ),
            )
        else:
            builder.note(
                "cot_neutral", "التموضع ضمن نطاقه الطبيعي",
                f"الصافي عند المئين {rank:.0%} — لا إشارة تطرف",
            )

        # --- commercial hedgers ---------------------------------------
        if "comm_net" in frame:
            comm = frame["comm_net"].dropna()
            if len(comm) >= 52:
                comm_latest = float(comm.iloc[-1])
                comm_hist = comm.tail(156)
                comm_rank = float((comm_hist <= comm_latest).sum() - 1) / max(1, len(comm_hist) - 1)
                # commercials are the natural hedgers; they lean against the move
                if comm_rank >= 0.85 or comm_rank <= 0.15:
                    builder.add(
                        "cot_commercials", "تموضع المتعاملين التجاريين",
                        clamp((comm_rank - 0.5) * 1.6) * 0.6, 0.8,
                        detail_ar=f"صافي التجاريين عند المئين {comm_rank:.0%} من تاريخه",
                    )
                metrics["comm_net_percentile"] = round(comm_rank, 4)

        # --- staleness -------------------------------------------------
        age_days = (ctx.as_of - frame.index[-1].to_pydatetime()).days
        metrics["report_age_days"] = float(age_days)
        quality = 1.0
        if age_days > 10:
            quality *= max(0.25, 1.0 - (age_days - 10) / 20.0)
        quality *= min(1.0, len(net) / 104.0)

        notes = [
            f"تقرير COT مؤرَّخ {frame.index[-1]:%Y-%m-%d} — أي بتأخر {age_days} يوماً عن السعر الحالي"
        ]
        return builder.result(self.id, quality=quality, metrics=metrics, notes_ar=notes)
