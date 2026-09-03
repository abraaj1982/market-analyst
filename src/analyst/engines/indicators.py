"""Technical indicator engine.

Oscillators are the easiest thing to misuse in an automated system, because the
same RSI value means opposite things in a trend and in a range. This engine
therefore reads every indicator *through* the regime:

  * In a **trend**, an overbought RSI is confirmation, not a sell signal, and
    only divergence counts against the move.
  * In a **range**, extremes are mean-reversion signals and get the opposite
    sign.

That single distinction is why the aggregator also de-weights this engine in
trending regimes: it is doing the right thing, but the trend engine is doing a
better job of the same question.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analyst.core.config import Settings
from analyst.core.enums import EngineId, Regime
from analyst.core.models import EngineResult, MarketContext
from analyst.engines.base import Engine, ScoreBuilder, scale
from analyst.indicators.oscillators import bollinger, macd, percentile_rank, rsi, stochastic
from analyst.indicators.structure import SwingKind, find_swings
from analyst.indicators.trend import atr_percent


class IndicatorEngine(Engine):
    id = EngineId.INDICATORS

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _run(self, ctx: MarketContext) -> EngineResult:
        tfs = sorted(ctx.series, key=lambda tf: tf.minutes)
        tf = tfs[len(tfs) // 2]
        df = ctx.series[tf].df
        if len(df) < 120:
            return EngineResult.skipped(self.id, f"تاريخ غير كافٍ على {tf.arabic}")

        close, high, low = df["close"], df["high"], df["low"]
        trending = ctx.regime in (Regime.TRENDING, Regime.HIGH_VOLATILITY)

        builder = ScoreBuilder()
        metrics: dict[str, float] = {}

        # --- RSI -------------------------------------------------------
        rsi_series = rsi(close, 14)
        rsi_now = float(rsi_series.iloc[-1])
        metrics["rsi"] = round(rsi_now, 2)
        if trending:
            # momentum reading: distance from 50, same sign as the move
            builder.add(
                "rsi_momentum", "زخم RSI",
                scale(rsi_now - 50.0, 30.0), 1.0,
                detail_ar=f"RSI عند {rsi_now:.1f} — يُقرأ كزخم لأن السوق اتجاهي",
            )
        else:
            # mean reversion: extremes push against the current move
            builder.add(
                "rsi_reversion", "ارتداد RSI من طرف",
                -scale(rsi_now - 50.0, 25.0), 1.0,
                detail_ar=f"RSI عند {rsi_now:.1f} — يُقرأ كارتداد لأن السوق عرضي",
            )

        divergence, div_detail = self._rsi_divergence(df, rsi_series)
        if divergence:
            builder.add("rsi_divergence", "دايفرجنس RSI", divergence, 1.3, detail_ar=div_detail)
            metrics["rsi_divergence"] = divergence

        # --- MACD ------------------------------------------------------
        macd_frame = macd(close)
        hist = macd_frame["hist"]
        hist_now = float(hist.iloc[-1])
        hist_prev = float(hist.iloc[-2])
        hist_scale = float(hist.abs().tail(120).quantile(0.80)) or 1e-9
        builder.add(
            "macd_histogram", "هيستوجرام MACD",
            scale(hist_now, hist_scale), 0.9,
            detail_ar=f"الهيستوجرام {hist_now:+.5f} ({'يتوسّع' if abs(hist_now) > abs(hist_prev) else 'ينكمش'})",
        )
        if np.sign(hist_now) != np.sign(hist_prev) and hist_now != 0:
            builder.add(
                "macd_cross", "تقاطع MACD جديد",
                float(np.sign(hist_now)) * 0.6, 0.8,
                detail_ar=f"تقاطع {'صاعد' if hist_now > 0 else 'هابط'} على الشمعة الأخيرة",
            )
        metrics["macd_hist"] = round(hist_now, 6)

        # --- Bollinger -------------------------------------------------
        bb = bollinger(close, 20, 2.0)
        pct_b = float(bb["percent_b"].iloc[-1])
        bandwidth = float(bb["bandwidth"].iloc[-1])
        metrics["percent_b"] = round(pct_b, 3)
        metrics["bandwidth"] = round(bandwidth, 5)
        if trending:
            builder.add(
                "bollinger_ride", "ركوب الحزام العلوي/السفلي",
                scale(pct_b - 0.5, 0.5) * 0.6, 0.7,
                detail_ar=f"%B عند {pct_b:.2f} — السير على الحزام تأكيد للاتجاه",
            )
        else:
            builder.add(
                "bollinger_reversion", "ارتداد من حزام بولنجر",
                -scale(pct_b - 0.5, 0.45), 0.9,
                detail_ar=f"%B عند {pct_b:.2f} — الأطراف تعني ارتداداً في السوق العرضي",
            )

        bw_rank = percentile_rank(bb["bandwidth"], min(252, len(df) // 2))
        bw_now = float(bw_rank.iloc[-1]) if pd.notna(bw_rank.iloc[-1]) else 0.5
        if bw_now <= 0.12:
            builder.note(
                "bollinger_squeeze", "انضغاط حزام بولنجر",
                f"عرض الحزام في أدنى {bw_now:.0%} من تاريخه — انفجار سعري محتمل بلا اتجاه محدد",
            )
        metrics["bandwidth_percentile"] = round(bw_now, 3)

        # --- Stochastic ------------------------------------------------
        stoch = stochastic(high, low, close)
        k_now, d_now = float(stoch["k"].iloc[-1]), float(stoch["d"].iloc[-1])
        if not trending and (k_now <= 20 or k_now >= 80):
            builder.add(
                "stochastic_extreme", "ستوكاستك عند طرف",
                -scale(k_now - 50.0, 35.0) * 0.8, 0.6,
                detail_ar=f"%K عند {k_now:.0f} و%D عند {d_now:.0f}",
            )
        metrics["stoch_k"] = round(k_now, 2)

        # --- volatility context ---------------------------------------
        atr_series = atr_percent(high, low, close, 14)
        atr_pct = percentile_rank(atr_series, min(252, len(df) // 2))
        atr_rank = float(atr_pct.iloc[-1]) if pd.notna(atr_pct.iloc[-1]) else 0.5
        metrics["atr_percentile"] = round(atr_rank, 3)
        if atr_rank >= 0.92:
            builder.note("volatility_extreme", "تقلب عند طرف تاريخي",
                         f"ATR في أعلى {atr_rank:.0%} من تاريخه — وقف الخسارة يحتاج مسافة أكبر")

        quality = min(1.0, len(df) / 250.0)
        return builder.result(self.id, quality=quality, metrics=metrics)

    # ------------------------------------------------------------------ #

    @staticmethod
    def _rsi_divergence(df: pd.DataFrame, rsi_series: pd.Series, lookback: int = 60) -> tuple[float, str]:
        """Regular divergence between the last two confirmed price pivots.

        Confirmed pivots only — reading divergence off an unconfirmed high is
        how divergence indicators end up repainting.
        """
        window = df.tail(lookback)
        swings = find_swings(window, 3, 3)
        if len(swings) < 4:
            return 0.0, ""

        offset = len(df) - len(window)
        rsi_vals = rsi_series.to_numpy()

        highs = [s for s in swings if s.kind is SwingKind.HIGH][-2:]
        lows = [s for s in swings if s.kind is SwingKind.LOW][-2:]

        if len(highs) == 2:
            p1, p2 = highs
            r1, r2 = rsi_vals[offset + p1.index], rsi_vals[offset + p2.index]
            if p2.price > p1.price and r2 < r1 - 2:
                return -0.8, (
                    f"قمة سعرية أعلى ({p2.price:.4f}) مقابل قمة RSI أدنى "
                    f"({r2:.0f} مقابل {r1:.0f}) — دايفرجنس هابط"
                )
        if len(lows) == 2:
            p1, p2 = lows
            r1, r2 = rsi_vals[offset + p1.index], rsi_vals[offset + p2.index]
            if p2.price < p1.price and r2 > r1 + 2:
                return 0.8, (
                    f"قاع سعري أدنى ({p2.price:.4f}) مقابل قاع RSI أعلى "
                    f"({r2:.0f} مقابل {r1:.0f}) — دايفرجنس صاعد"
                )
        return 0.0, ""
