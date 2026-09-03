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
        return False, "Alerts are disabled in settings"

    minimum = Grade(settings.min_grade)
    if _GRADE_RANK[result.grade] < _GRADE_RANK[minimum]:
        return False, f"Grade {result.grade.value} is below the {minimum.value} floor"

    if result.blocking_failures:
        return False, f"Hard gate not satisfied: {result.blocking_failures[0].label}"

    window = timedelta(minutes=settings.cooldown_minutes)
    previous = last_alert_state(result.symbol, window)
    if previous is None:
        return True, "First alert for this symbol inside the cooldown window"

    sent_at, direction, grade_value = previous
    if not settings.require_state_change:
        return False, f"Previous alert sent {sent_at:%Y-%m-%d %H:%M} — still in cooldown"

    if direction != int(result.direction.value):
        return True, "Direction flipped since the previous alert"

    try:
        previous_grade = Grade(grade_value)
    except ValueError:
        return True, "Previous alert grade is unrecognised"

    if _GRADE_RANK[result.grade] > _GRADE_RANK[previous_grade]:
        return True, f"Grade upgraded from {previous_grade.value} to {result.grade.value}"

    return False, (
        f"No material change since {sent_at:%Y-%m-%d %H:%M} "
        f"({previous_grade.value} -> {result.grade.value})"
    )
