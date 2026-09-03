"""Trend engine — multi-timeframe directional bias.

Reads four independent trend measures on every available timeframe, collapses
each timeframe into one score, then combines timeframes using the profile's MTF
weights (daily carries the most in the swing profile).

Measures per timeframe:
  1. **EMA stack** — price versus 20/50/200 and their ordering. The single most
     robust trend read there is; deliberately the heaviest component.
  2. **EMA slope** — normalised by ATR so it is comparable across instruments.
  3. **Ichimoku** — price versus cloud, and Tenkan versus Kijun.
  4. **Market structure** — HH/HL versus LH/LL from confirmed swing pivots.

The engine also reports `mtf_agreement`: how many timeframes point the same way.
That number is what the MTF alignment gate reads, and it is why a signal that
looks strong on one timeframe alone never reaches A grade.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analyst.core.config import Settings
from analyst.core.enums import Direction, EngineId, Timeframe
from analyst.core.models import EngineResult, MarketContext
from analyst.engines.base import Engine, ScoreBuilder, clamp, scale
from analyst.indicators.structure import classify_structure, find_swings
from analyst.indicators.trend import atr, ema, ichimoku, slope_normalised


class TrendEngine(Engine):
    id = EngineId.TREND

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _run(self, ctx: MarketContext) -> EngineResult:
        weights = self.settings.active_profile.normalised_mtf_weights
        builder = ScoreBuilder()
        per_tf: dict[Timeframe, float] = {}
        metrics: dict[str, float] = {}

        for tf, weight in weights.items():
            series = ctx.get(tf)
            if series is None or len(series) < 210:
                continue
            tf_score, tf_metrics, tf_detail = self._score_timeframe(series.df)
            per_tf[tf] = tf_score
            metrics.update({f"{tf.value}_{k}": v for k, v in tf_metrics.items()})
            builder.add(
                f"trend_{tf.value}",
                f"اتجاه {tf.arabic}",
                tf_score,
                weight,
                detail_ar=tf_detail,
                record_when_zero=True,
            )

        if not per_tf:
            return EngineResult.skipped(self.id, "لا يوجد فريم واحد بتاريخ كافٍ (200 شمعة على الأقل)")

        directions = [np.sign(v) for v in per_tf.values() if abs(v) >= 0.10]
        agreement = (
            abs(sum(directions)) / len(directions) if directions else 0.0
        )
        metrics["mtf_agreement"] = agreement
        metrics["timeframes_scored"] = float(len(per_tf))
        for tf, v in per_tf.items():
            metrics[f"{tf.value}_score"] = v

        if directions and agreement == 1.0:
            builder.note(
                "mtf_full_agreement",
                "توافق كامل بين كل الفريمات",
                f"{len(directions)} فريمات تشير لنفس الاتجاه",
                Direction.from_score(directions[0], 0.0),
            )
        elif agreement < 0.4:
            builder.note("mtf_conflict", "تعارض بين الفريمات",
                         "الفريمات لا تتفق — قوة الإشارة مخفّضة")

        # Quality reflects how much of the intended MTF weight actually had data.
        covered = sum(weights[tf] for tf in per_tf)
        return builder.result(self.id, quality=clamp(covered, 0.0, 1.0), metrics=metrics)

    # ------------------------------------------------------------------ #

    @staticmethod
    def _score_timeframe(df: pd.DataFrame) -> tuple[float, dict[str, float], str]:
        close, high, low = df["close"], df["high"], df["low"]
        price = float(close.iloc[-1])
        atr_series = atr(high, low, close, 14)
        atr_now = float(atr_series.iloc[-1])

        ema20 = ema(close, 20)
        ema50 = ema(close, 50)
        ema200 = ema(close, 200)
        e20, e50, e200 = (float(s.iloc[-1]) for s in (ema20, ema50, ema200))

        # 1. EMA stack: position (half) + ordering (half)
        position = np.mean([price > e20, price > e50, price > e200]) * 2 - 1
        if e20 > e50 > e200:
            ordering = 1.0
        elif e20 < e50 < e200:
            ordering = -1.0
        else:
            ordering = clamp((np.sign(e20 - e50) + np.sign(e50 - e200)) / 2.0)
        ema_score = 0.5 * position + 0.5 * ordering

        # 2. slope of the mid EMA, in ATR units per bar
        slope = slope_normalised(ema50, 10, atr_series).iloc[-1]
        slope_score = scale(float(slope) if pd.notna(slope) else 0.0, 0.20)

        # 3. Ichimoku
        ich = ichimoku(high, low, close)
        cloud_top = float(ich["cloud_top"].iloc[-1])
        cloud_bottom = float(ich["cloud_bottom"].iloc[-1])
        tenkan, kijun = float(ich["tenkan"].iloc[-1]), float(ich["kijun"].iloc[-1])
        if price > cloud_top:
            cloud = 1.0
        elif price < cloud_bottom:
            cloud = -1.0
        else:
            span = max(cloud_top - cloud_bottom, 1e-9)
            cloud = clamp(((price - cloud_bottom) / span) * 2 - 1) * 0.4  # inside = weak
        tk_cross = clamp(np.sign(tenkan - kijun) * min(1.0, abs(tenkan - kijun) / max(atr_now, 1e-9)))
        ichimoku_score = 0.65 * cloud + 0.35 * tk_cross

        # 4. structure from confirmed pivots
        swings = find_swings(df.tail(300), left=2, right=2)
        struct_dir, struct_label = classify_structure(swings)

        score = float(
            0.38 * ema_score + 0.18 * slope_score + 0.22 * ichimoku_score + 0.22 * struct_dir
        )

        metrics = {
            "ema_score": round(ema_score, 3),
            "slope_score": round(slope_score, 3),
            "ichimoku_score": round(ichimoku_score, 3),
            "structure": float(struct_dir),
            "ema20": round(e20, 5), "ema50": round(e50, 5), "ema200": round(e200, 5),
            "atr": round(atr_now, 5),
        }
        stack = "متتالية صاعدة" if e20 > e50 > e200 else (
            "متتالية هابطة" if e20 < e50 < e200 else "متشابكة"
        )
        detail = f"EMA {stack} · {struct_label} · السعر {'فوق' if price > cloud_top else 'تحت' if price < cloud_bottom else 'داخل'} سحابة إيشيموكو"
        return clamp(score), metrics, detail
