"""News / economic-calendar engine.

This engine deliberately never votes on direction. Nobody knows whether a CPI
print will come in hot or cold, and a system that pretends otherwise is
manufacturing confidence out of thin air.

What it does instead:
  * classifies the current news environment (clear / caution / blackout)
  * exposes `news_factor`, a multiplier in (0, 1] that the aggregator applies to
    the final confidence — it can only ever *reduce* it
  * feeds the `news_blackout` hard gate

Its weight in `settings.yaml` is 0 by design. It is a modifier and a gate, not a
voter.
"""
from __future__ import annotations

from datetime import timedelta

from analyst.core.config import Settings
from analyst.core.enums import Direction, EngineId
from analyst.core.models import EngineResult, Evidence, MarketContext
from analyst.engines.base import Engine


class NewsEngine(Engine):
    id = EngineId.NEWS

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _run(self, ctx: MarketContext) -> EngineResult:
        blackout = ctx.extras.get("calendar_blackout")
        upcoming = ctx.extras.get("next_high_impact")
        events = ctx.extras.get("calendar_events") or []

        evidence: list[Evidence] = []
        metrics: dict[str, float] = {"events_in_window": float(len(events))}
        notes: list[str] = []

        if blackout is not None:
            factor = self.settings.scoring.news_penalty_high
            evidence.append(
                Evidence(
                    code="news_blackout",
                    label_ar="🔴 نافذة حظر أخبار نشطة",
                    detail_ar=f"{blackout.label_ar} — التوقيت {blackout.when:%Y-%m-%d %H:%M} UTC",
                    direction=Direction.NEUTRAL,
                )
            )
            notes.append("الدخول محظور الآن: خبر عالي الأثر داخل نافذة الحظر")
        elif upcoming is not None:
            hours = (upcoming.when - ctx.as_of).total_seconds() / 3600.0
            if hours <= 4:
                factor = self.settings.scoring.news_penalty_high
                label = "🔴 خبر عالي الأثر خلال أقل من 4 ساعات"
            elif hours <= 24:
                factor = self.settings.scoring.news_penalty_medium
                label = "🟡 خبر عالي الأثر خلال 24 ساعة"
            else:
                factor = 1.0
                label = "🟢 لا أخبار عالية الأثر قريبة"
            evidence.append(
                Evidence(
                    code="news_upcoming",
                    label_ar=label,
                    detail_ar=(
                        f"{upcoming.label_ar} بعد {hours:.1f} ساعة "
                        f"({upcoming.when:%Y-%m-%d %H:%M} UTC)"
                    ),
                    direction=Direction.NEUTRAL,
                )
            )
            metrics["hours_to_next_high_impact"] = round(hours, 2)
        else:
            factor = 1.0
            evidence.append(
                Evidence(
                    code="news_clear",
                    label_ar="🟢 الأجندة نظيفة",
                    detail_ar="لا أحداث عالية الأثر خلال الـ72 ساعة القادمة",
                    direction=Direction.NEUTRAL,
                )
            )

        for event in events[:5]:
            if event.when < ctx.as_of - timedelta(hours=1):
                continue
            evidence.append(
                Evidence(
                    code=f"event_{event.id}",
                    label_ar=f"{event.emoji} {event.label_ar}",
                    detail_ar=f"{event.when:%Y-%m-%d %H:%M} UTC · أثر {event.impact_ar}",
                    direction=Direction.NEUTRAL,
                )
            )

        metrics["news_factor"] = round(factor, 4)
        return EngineResult(
            engine=self.id,
            direction=Direction.NEUTRAL,   # never votes on direction, by design
            strength=0.0,
            quality=1.0 if ctx.extras.get("calendar_events") is not None else 0.0,
            evidence=evidence,
            metrics=metrics,
            notes_ar=notes,
        )
