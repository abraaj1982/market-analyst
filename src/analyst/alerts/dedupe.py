"""Alert suppression.

An analyst who says the same thing every 30 minutes gets muted, and a muted
alert channel is worth nothing when the one that matters arrives. Two rules:

  * **Cooldown** — never repeat the same symbol within the configured window.
  * **State change** — inside a longer window, only speak again if the direction
    flipped or the grade improved. A setup that merely persists is not news.
"""
from __future__ import annotations

from datetime import timedelta

from analyst.core.config import AlertSettings
from analyst.core.enums import Grade
from analyst.core.models import AnalysisResult
from analyst.storage.analyses import last_alert_state

_GRADE_RANK = {Grade.NO_TRADE: 0, Grade.C: 1, Grade.B: 2, Grade.A: 3, Grade.A_PLUS: 4}


def should_alert(result: AnalysisResult, settings: AlertSettings) -> tuple[bool, str]:
    """Return (send?, reason). The reason is logged either way."""
    if not settings.enabled:
        return False, "التنبيهات معطّلة في الإعدادات"

    minimum = Grade(settings.min_grade)
    if _GRADE_RANK[result.grade] < _GRADE_RANK[minimum]:
        return False, f"التصنيف {result.grade.value} دون الحد الأدنى {minimum.value}"

    if result.blocking_failures:
        return False, f"بوابة صلبة لم تتحقق: {result.blocking_failures[0].label_ar}"

    window = timedelta(minutes=settings.cooldown_minutes)
    previous = last_alert_state(result.symbol, window)
    if previous is None:
        return True, "أول تنبيه لهذا الرمز داخل نافذة التهدئة"

    sent_at, direction, grade_value = previous
    if not settings.require_state_change:
        return False, f"تنبيه سابق أُرسل في {sent_at:%Y-%m-%d %H:%M} — داخل نافذة التهدئة"

    if direction != int(result.direction.value):
        return True, "انعكاس الاتجاه عن التنبيه السابق"

    try:
        previous_grade = Grade(grade_value)
    except ValueError:
        return True, "تصنيف التنبيه السابق غير معروف"

    if _GRADE_RANK[result.grade] > _GRADE_RANK[previous_grade]:
        return True, f"ترقية التصنيف من {previous_grade.value} إلى {result.grade.value}"

    return False, (
        f"لا تغيّر جوهري منذ تنبيه {sent_at:%Y-%m-%d %H:%M} "
        f"({previous_grade.value} → {result.grade.value})"
    )
