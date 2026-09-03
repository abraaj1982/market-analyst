"""Economic calendar without a paid subscription.

Most calendar APIs are behind paywalls, and scraping calendar sites is fragile
and against their terms. This module instead does something boring and durable:

  * **Recurring releases** (NFP, CPI, PPI, jobless claims, retail sales) follow
    published, stable scheduling rules — first Friday, second Wednesday, and so
    on. Those are generated deterministically.
  * **Irregular events** (FOMC, Jackson Hole) do not follow a rule, so they live
    in `config/calendar.yaml` and take two minutes to refresh once a year from
    the Federal Reserve's own published calendar.

The engine treats this as a *blackout gate*, not a directional signal: nobody
can predict a print, so the only honest use of a calendar is to stand aside
around it.
"""
from __future__ import annotations

import calendar as _cal
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from analyst.core.clock import UTC
from analyst.core.config import CONFIG_DIR

IMPACT_RANK = {"low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    id: str
    label: str
    impact: str
    currency: str
    when: datetime

    @property
    def impact_label(self) -> str:
        return {"low": "low", "medium": "medium", "high": "high"}[self.impact]

    @property
    def emoji(self) -> str:
        return {"low": "🟢", "medium": "🟡", "high": "🔴"}[self.impact]


class EconomicCalendar:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CONFIG_DIR / "calendar.yaml"
        self._spec = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}

    def events_between(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        """All known events in [start, end], sorted chronologically."""
        events: list[EconomicEvent] = []
        events.extend(self._fixed_events(start, end))
        events.extend(self._recurring_events(start, end))
        return sorted((e for e in events if start <= e.when <= end), key=lambda e: e.when)

    def next_high_impact(self, now: datetime, horizon_hours: int = 72) -> EconomicEvent | None:
        upcoming = [
            e for e in self.events_between(now, now + timedelta(hours=horizon_hours))
            if e.impact == "high"
        ]
        return upcoming[0] if upcoming else None

    def in_blackout(
        self, now: datetime, minutes_before: int = 60, minutes_after: int = 30
    ) -> EconomicEvent | None:
        """The high-impact event whose blackout window contains `now`, if any."""
        window = self.events_between(
            now - timedelta(minutes=minutes_after), now + timedelta(minutes=minutes_before)
        )
        for e in window:
            if e.impact != "high":
                continue
            if e.when - timedelta(minutes=minutes_before) <= now <= e.when + timedelta(
                minutes=minutes_after
            ):
                return e
        return None

    # ------------------------------------------------------------------ #

    def _fixed_events(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        out: list[EconomicEvent] = []
        for spec in self._spec.get("fixed", []):
            for raw in spec.get("datetimes_utc", []):
                when = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
                if start <= when <= end:
                    out.append(
                        EconomicEvent(
                            spec["id"], spec["label"], spec["impact"],
                            spec.get("currency", "USD"), when,
                        )
                    )
        return out

    def _recurring_events(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        out: list[EconomicEvent] = []
        for spec in self._spec.get("recurring", []):
            hh, mm = (int(x) for x in spec.get("time_utc", "13:30").split(":"))
            at = time(hh, mm, tzinfo=UTC)
            for day in self._rule_dates(spec, start.date(), end.date()):
                out.append(
                    EconomicEvent(
                        spec["id"], spec["label"], spec["impact"],
                        spec.get("currency", "USD"),
                        datetime.combine(day, at),
                    )
                )
        return out

    @staticmethod
    def _rule_dates(spec: dict, start: date, end: date) -> list[date]:
        rule = spec.get("rule")
        days: list[date] = []

        if rule == "weekly":
            weekday = int(spec.get("weekday", 3))
            cursor = start
            while cursor <= end:
                if cursor.weekday() == weekday:
                    days.append(cursor)
                cursor += timedelta(days=1)
            return days

        # month-anchored rules: walk the months the window touches
        month = date(start.year, start.month, 1)
        while month <= end:
            if rule == "first_friday":
                target = EconomicCalendar._nth_weekday(month.year, month.month, 4, 1)
            elif rule == "nth_weekday":
                target = EconomicCalendar._nth_weekday(
                    month.year, month.month, int(spec.get("weekday", 2)), int(spec.get("nth", 2))
                )
            else:
                target = None
            if target and start <= target <= end:
                days.append(target)
            month = date(
                month.year + (month.month == 12), (month.month % 12) + 1, 1
            )
        return days

    @staticmethod
    def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date | None:
        """`nth` occurrence (1-based) of `weekday` (0=Mon) in the given month."""
        first_weekday, days_in_month = _cal.monthrange(year, month)
        offset = (weekday - first_weekday) % 7
        day = 1 + offset + (nth - 1) * 7
        return date(year, month, day) if day <= days_in_month else None
