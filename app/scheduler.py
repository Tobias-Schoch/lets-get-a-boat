from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from .crawler import run_crawl
from .db import SessionLocal, get_settings
from .models import Target

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 30
_scheduler: AsyncIOScheduler | None = None


def _job_id(target_id: int) -> str:
    return f"target-{target_id}"


def _trigger(minutes: int) -> IntervalTrigger:
    return IntervalTrigger(minutes=max(1, int(minutes)))


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def global_interval_minutes() -> int:
    with SessionLocal() as s:
        settings = get_settings(s)
        return max(1, settings.crawl_interval_minutes or DEFAULT_INTERVAL_MINUTES)


def start() -> None:
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
    reschedule_all()


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def upsert_job(target_id: int, interval_minutes: int | None = None) -> None:
    scheduler = get_scheduler()
    job_id = _job_id(target_id)
    minutes = interval_minutes if interval_minutes is not None else global_interval_minutes()
    trigger = _trigger(minutes)
    if scheduler.get_job(job_id) is None:
        scheduler.add_job(
            run_crawl,
            trigger=trigger,
            args=[target_id],
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("scheduled target %s every %s min", target_id, minutes)
    else:
        scheduler.reschedule_job(job_id, trigger=trigger)
        logger.info("rescheduled target %s to every %s min", target_id, minutes)


def remove_job(target_id: int) -> None:
    scheduler = get_scheduler()
    job_id = _job_id(target_id)
    if scheduler.get_job(job_id) is not None:
        scheduler.remove_job(job_id)


def reschedule_all() -> None:
    """Apply the current global interval to every enabled target's job.

    Disabled targets have their jobs removed. Call after settings change.
    """
    minutes = global_interval_minutes()
    with SessionLocal() as s:
        targets = s.execute(select(Target)).scalars().all()
        for t in targets:
            if t.enabled:
                upsert_job(t.id, minutes)
            else:
                remove_job(t.id)


def next_run_at(target_id: int) -> datetime | None:
    scheduler = get_scheduler()
    job = scheduler.get_job(_job_id(target_id))
    return job.next_run_time if job is not None else None
