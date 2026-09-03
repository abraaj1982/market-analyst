"""Volume / Wyckoff and seasonality engine.

**Volume** is only read when it is real. Spot FX has no consolidated volume and
Yahoo reports zeros or tick counts for it; scoring that would be scoring noise,
so the engine detects the absence and stands aside for those instruments rather
than producing a confident-looking number from nothing.

Where volume is real (equities, indices, futures) it reads two Wyckoff ideas
that survive mechanisation:

  * **Effort versus result** — a large range on low volume, or a small range on
    huge volume, marks absorption. The second is the more useful: heavy volume
    that fails to move price means someone large is taking the other side.
  * **Volume confirmation** — advances on expanding volume and pullbacks on
    contracting volume describe a healthy trend.

**Seasonality** is computed from the instrument's own stored daily history —
month-of-year and day-of-week mean returns. It carries a small weight and is
reported with its sample size, because a "seasonal edge" from six observations
is not an edge.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from analyst.core.config import Settings
from analyst.core.enums import EngineId, Timeframe
from analyst.core.models import EngineResult, MarketContext
from analyst.engines.base import Engine, ScoreBuilder, clamp, scale

#: Minimum observations before a seasonal average is allowed to score at all.
MIN_SEASONAL_SAMPLES = 8


class VolumeSeasonalityEngine(Engine):
    id = EngineId.VOLUME_SEASONALITY

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _run(self, ctx: MarketContext) -> EngineResult:
        builder = ScoreBuilder()
        metrics: dict[str, float] = {}
        notes: list[str] = []

        tf = max(ctx.series, key=lambda t: t.minutes)
        df = ctx.series[tf].df

        has_volume = self._volume_is_real(df)
        metrics["volume_available"] = float(has_volume)

        if has_volume:
            self._score_volume(builder, metrics, df)
        else:
            notes.append(
                "لا توجد أحجام تداول حقيقية لهذا الرمز (طبيعي في الفوركس الفوري) — "
                "تم تعطيل الجزء الحجمي وقُيّم الموسمية فقط"
            )

        daily = ctx.get(Timeframe.D1)
        seasonal_samples = 0
        if daily is not None and len(daily) >= 260:
            seasonal_samples = self._score_seasonality(builder, metrics, daily.df, ctx.as_of)

        if not builder.items and builder.weight_total == 0:
            return EngineResult.skipped(self.id, "لا أحجام حقيقية ولا تاريخ يومي كافٍ للموسمية")

        quality = (0.65 if has_volume else 0.0) + (
            0.35 * min(1.0, seasonal_samples / 15.0) if seasonal_samples else 0.0
        )
        return builder.result(self.id, quality=max(quality, 0.15), metrics=metrics, notes_ar=notes)

    # ------------------------------------------------------------------ #

    @staticmethod
    def _volume_is_real(df: pd.DataFrame) -> bool:
        """Reject all-zero, constant, or near-empty volume columns."""
        vol = df["volume"].tail(200)
        if vol.empty or float(vol.sum()) <= 0:
            return False
        if float((vol == 0).mean()) > 0.35:
            return False
        return float(vol.std()) > 0

    @staticmethod
    def _score_volume(builder: ScoreBuilder, metrics: dict, df: pd.DataFrame) -> None:
        vol = df["volume"]
        close = df["close"]
        rng = df["high"] - df["low"]

        vol_ma = vol.rolling(20, min_periods=10).mean()
        vol_ratio = float(vol.iloc[-1] / max(float(vol_ma.iloc[-1]), 1e-9))
        ret = float(close.iloc[-1] / close.iloc[-2] - 1.0) if len(close) > 1 else 0.0
        metrics["volume_ratio"] = round(vol_ratio, 3)

        # effort vs result: normalise the bar's range by its own recent average
        rng_ma = rng.rolling(20, min_periods=10).mean()
        rng_ratio = float(rng.iloc[-1] / max(float(rng_ma.iloc[-1]), 1e-9))
        metrics["range_ratio"] = round(rng_ratio, 3)

        if vol_ratio >= 1.8 and rng_ratio <= 0.7:
            # heavy effort, no result: absorption against the last move
            builder.add(
                "wyckoff_absorption", "امتصاص (جهد كبير بنتيجة ضعيفة)",
                -np.sign(ret) * 0.7 if ret != 0 else 0.0, 1.2,
                detail_ar=(
                    f"حجم {vol_ratio:.1f}× المتوسط بمدى سعري {rng_ratio:.1f}× فقط — "
                    "طرف كبير يمتص الحركة"
                ),
            )
        else:
            builder.add(
                "volume_confirmation", "تأكيد حجمي للحركة",
                clamp(np.sign(ret) * scale(vol_ratio - 1.0, 1.0)), 1.0,
                detail_ar=f"الحجم {vol_ratio:.1f}× المتوسط مع حركة {ret:+.2%}",
            )

        # trend of participation over the last 10 bars
        recent_vol = float(vol.tail(10).mean())
        prior_vol = float(vol.tail(40).head(30).mean()) or 1e-9
        participation = recent_vol / prior_vol - 1.0
        price_change = float(close.iloc[-1] / close.iloc[-11] - 1.0) if len(close) > 11 else 0.0
        builder.add(
            "participation_trend", "اتجاه المشاركة الحجمية",
            clamp(np.sign(price_change) * scale(participation, 0.6)) * 0.6, 0.8,
            detail_ar=(
                f"متوسط الحجم في آخر 10 شموع {participation:+.0%} مقارنة بالسابق "
                f"مع تغيّر سعري {price_change:+.2%}"
            ),
        )
        metrics["participation_change"] = round(participation, 3)

    @staticmethod
    def _score_seasonality(builder: ScoreBuilder, metrics: dict, daily: pd.DataFrame, as_of) -> int:
        returns = daily["close"].pct_change().dropna()
        if returns.empty:
            return 0

        month = as_of.month
        weekday = as_of.weekday()

        month_returns = returns[returns.index.month == month]
        dow_returns = returns[returns.index.dayofweek == weekday]
        samples = len(month_returns)

        if samples >= MIN_SEASONAL_SAMPLES:
            mean = float(month_returns.mean())
            overall_std = float(returns.std()) or 1e-9
            # a t-like statistic: mean move relative to noise and sample size
            t_stat = mean / (overall_std / np.sqrt(samples))
            builder.add(
                "seasonality_month", "الموسمية الشهرية",
                scale(t_stat, 2.5) * 0.6, 0.9,
                detail_ar=(
                    f"متوسط العائد اليومي في هذا الشهر {mean:+.3%} عبر {samples} ملاحظة "
                    f"(إحصائية t = {t_stat:+.2f})"
                ),
            )
            metrics["seasonal_month_mean"] = round(mean, 6)
            metrics["seasonal_month_samples"] = float(samples)

        if len(dow_returns) >= 30:
            dow_mean = float(dow_returns.mean())
            builder.add(
                "seasonality_weekday", "الموسمية حسب يوم الأسبوع",
                scale(dow_mean / (float(returns.std()) or 1e-9), 0.25) * 0.4, 0.5,
                detail_ar=f"متوسط عائد هذا اليوم من الأسبوع {dow_mean:+.3%} عبر {len(dow_returns)} ملاحظة",
            )
            metrics["seasonal_dow_mean"] = round(dow_mean, 6)

        return samples
