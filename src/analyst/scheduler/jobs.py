"""Periodic jobs.

Three cadences, each matched to how fast its input actually changes:

  * **Analysis** — every `analysis_interval_minutes` from the active profile.
  * **Digest** — once a day at the configured local hour.
  * **Maintenance** — nightly candle pruning and outcome tracking.

`max_instances=1` and `coalesce=True` prevent a slow run from stacking up behind
itself, which on a free data tier is the difference between working and being
rate-limited into silence.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger(__name__)


def start_scheduler(service) -> BackgroundScheduler:
    settings = service.settings
    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 300},
    )

    interval = settings.active_profile.analysis_interval_minutes

    def analysis_job() -> None:
        try:
            results = service.run_once()
            actionable = sum(1 for r in results if r.is_actionable)
            log.info("دورة تحليل: %d رمز · %d فرصة مؤهلة", len(results), actionable)
        except Exception:
            log.exception("فشلت دورة التحليل")

    def digest_job() -> None:
        try:
            results = service.run_once(alert=False)
            service.send_digest(results)
            log.info("أُرسل التقرير اليومي")
        except Exception:
            log.exception("فشل التقرير اليومي")

    def maintenance_job() -> None:
        try:
            removed = service.prune()
            counts = service.tracker.update_open_signals()
            log.info("صيانة: حُذفت %d شمعة قديمة · نتائج الإشارات %s", removed, counts)
        except Exception:
            log.exception("فشلت الصيانة الليلية")

    scheduler.add_job(analysis_job, IntervalTrigger(minutes=interval), id="analysis",
                      name="دورة التحليل")
    scheduler.add_job(
        digest_job,
        CronTrigger(hour=settings.alerts.daily_digest_hour_local, minute=0,
                    timezone=settings.timezone.display),
        id="digest", name="التقرير اليومي",
    )
    scheduler.add_job(maintenance_job, CronTrigger(hour=2, minute=30, timezone="UTC"),
                      id="maintenance", name="الصيانة الليلية")

    scheduler.start()
    log.info("الجدولة بدأت — تحليل كل %d دقيقة", interval)
    return scheduler
