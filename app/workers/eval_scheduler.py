"""Accuracy eval-harness scheduler (2026-07-30) — weekly re-computation of
every metric in eval_harness.py, mirrors advice_scheduler.py's/notify_sub's
lifespan pattern EXACTLY: started once from main.py's lifespan, degrades to a
log line if APScheduler isn't installed, never blocks app startup and never
raises into the caller. Pure observability — no user-facing behaviour.

Cadence: Monday 05:00 Europe/Madrid (before the household wakes up, well
clear of the 20:20/21:00 api_exo/advice jobs the day before).
"""
from __future__ import annotations

import logging

from app.core.config import settings
from app.services import eval_harness

logger = logging.getLogger("consum.eval_scheduler")

_scheduler = None  # apscheduler.schedulers.asyncio.AsyncIOScheduler, set in start()


async def run_weekly() -> None:
    try:
        summary = await eval_harness.run_all()
        logger.info("eval_scheduler: weekly run OK — %s", summary["metrics"])
    except Exception as exc:  # noqa: BLE001 — a scheduled run must never crash the process
        logger.error("eval_scheduler: weekly run failed: %s", exc)


def start() -> None:
    """Wire the weekly cron job into an AsyncIOScheduler — called once from
    main.py's lifespan."""
    global _scheduler
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed — eval_scheduler disabled")
        return

    _scheduler = AsyncIOScheduler(timezone="Europe/Madrid")
    _scheduler.add_job(run_weekly, CronTrigger(day_of_week=settings.EVAL_WEEKLY_DOW,
                                               hour=settings.EVAL_WEEKLY_HOUR, minute=0),
                       id="eval_weekly", replace_existing=True, misfire_grace_time=1800)
    _scheduler.start()
    logger.info("eval_scheduler started: weekly=dow%d %02d:00 (Europe/Madrid)",
               settings.EVAL_WEEKLY_DOW, settings.EVAL_WEEKLY_HOUR)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
