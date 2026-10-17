"""Scheduler - the 'brain' that runs workflows on a schedule.

Uses APScheduler to run workflows automatically:
- Every day at 22:00 UTC (06:00 Asia/Shanghai): daily morning routine
- Every 6 hours: check for at-risk customers
- On-demand: manual trigger via API

The scheduler starts with the FastAPI app and runs in the background.

**What stops a job running twice.** Three things, and only the last one is a
guarantee:

1. APScheduler's ``max_instances=1`` refuses a second fire of the same job *in
   this process* while the first is still running. It does not cover the manual
   trigger (which calls the function directly) or a second process.
2. ``coalesce=True`` means a missed fire collapses into one, not a burst of
   catch-ups.
3. **The claim** (``app/services/job_guard.py``): a ``job_runs`` row with
   ``status='running'``, protected by a partial unique index, so the database
   itself refuses the second claim -- from any caller, in any process. Every
   entry point goes through it, including the manual trigger, and a refused
   attempt is *recorded* rather than dropped.

The third one exists because the first two are silent: a skipped fire used to
leave a log line and nothing else, which is indistinguishable from a report that
was never scheduled.

``misfire_grace_time`` is set explicitly (APScheduler's effective default is one
second). With a memory job store, a restart at 22:00:02 loses the day's run
entirely -- there is nothing to misfire from. An hour of grace means a restart
shortly after the scheduled time still produces the run, late and recorded.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

#: Global scheduler instance
_scheduler = None

#: Job identifiers, shared by the scheduler and the claim.
DAILY_ROUTINE_JOB = "daily_morning_routine"
CHURN_CHECK_JOB = "churn_check"

#: One hour, against APScheduler's one-second default. The trade is explicit: a
#: run that starts at 22:40 after a restart is better than no run at all, and the
#: record says when it started.
MISFIRE_GRACE_SECONDS = 3600


def get_scheduler():
    """Get or create the scheduler instance."""
    global _scheduler
    if _scheduler is None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.jobstores.memory import MemoryJobStore

        _scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": MISFIRE_GRACE_SECONDS,
            },
        )
    return _scheduler


def start_scheduler():
    """Start the scheduler with all registered jobs."""
    scheduler = get_scheduler()

    # Only add jobs if not already added
    if not scheduler.get_jobs():
        # Daily morning routine at 6:00 AM (Asia/Shanghai = UTC+8, so 22:00 UTC previous day)
        scheduler.add_job(
            _run_daily_routine,
            "cron",
            hour=22,
            minute=0,
            id=DAILY_ROUTINE_JOB,
            name="Daily Morning Report",
            replace_existing=True,
        )

        # Churn check every 6 hours
        scheduler.add_job(
            _run_churn_check,
            "interval",
            hours=6,
            id=CHURN_CHECK_JOB,
            name="Customer Churn Check",
            replace_existing=True,
        )

    scheduler.start()
    logger.info(f"Scheduler started with {len(scheduler.get_jobs())} jobs")
    for job in scheduler.get_jobs():
        logger.info(f"  - {job.name} (next run: {job.next_run_time})")


def stop_scheduler():
    """Stop the scheduler gracefully."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
        _scheduler = None


def get_job_status() -> list:
    """Get status of all scheduled jobs, including what actually happened."""
    scheduler = get_scheduler()
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else "Not scheduled",
            "trigger": str(job.trigger),
            "misfire_grace_seconds": MISFIRE_GRACE_SECONDS,
        })
    return jobs


async def get_job_history() -> dict:
    """Last attempts per job, so a skipped run is visible where people look."""
    from app.database import async_session
    from app.services import job_guard

    history = {}
    async with async_session() as db:
        for job_id in (DAILY_ROUTINE_JOB, CHURN_CHECK_JOB):
            history[job_id] = await job_guard.recent(db, job_id)
    return history


async def trigger_daily_routine(search_keywords: str = "textile buyer", target_country: str = ""):
    """Manually trigger the daily routine (for testing or on-demand use).

    Goes through the same claim as the scheduled fire. Before that it did not,
    so a human clicking "run now" while the cron was working produced two runs
    over the same book -- APScheduler's ``max_instances`` only ever governed its
    own fires.
    """
    logger.info("Manual trigger: daily routine")
    return await _run_daily_routine(
        search_keywords=search_keywords,
        target_country=target_country,
        trigger="manual",
    )


async def _run_daily_routine(
    search_keywords: str = "textile buyer",
    target_country: str = "",
    trigger: str = "schedule",
):
    """Internal function to run the daily routine, once.

    Three separate short sessions: claim, work, record. The work gets none of them
    held open, so a routine that takes minutes is not sitting on one of the
    fifteen pooled connections while it does it (see R17).
    """
    from app.database import async_session
    from app.models.job_run import FAILED, SUCCEEDED
    from app.services import job_guard

    async with async_session() as db:
        claimed, claim, why_not = await job_guard.try_claim(
            db, DAILY_ROUTINE_JOB, trigger=trigger
        )
    if not claimed:
        return {"skipped": True, "reason": why_not}

    run_id = claim.id
    try:
        from app.workflows import daily_morning_routine

        result = await daily_morning_routine(
            search_keywords=search_keywords,
            target_country=target_country,
        )
    except Exception as e:
        logger.error(f"Daily routine failed: {e}")
        import traceback

        traceback.print_exc()
        async with async_session() as db:
            await job_guard.finish(db, run_id, status=FAILED, detail=str(e)[:500])
        return {"error": str(e)}

    async with async_session() as db:
        await job_guard.finish(db, run_id, status=SUCCEEDED, detail="completed")
    return result


async def _run_churn_check(trigger: str = "schedule"):
    """Internal function to run churn check, once."""
    from app.database import async_session
    from app.models.job_run import FAILED, SUCCEEDED
    from app.services import job_guard

    async with async_session() as db:
        claimed, claim, why_not = await job_guard.try_claim(
            db, CHURN_CHECK_JOB, trigger=trigger
        )
    if not claimed:
        return {"skipped": True, "reason": why_not}

    run_id = claim.id
    try:
        from app.services.agent_service import customer_churn_alerts
        from app.notification import send_notification

        async with async_session() as db:
            result = await customer_churn_alerts(db_session=db)
        critical = result.get("critical", 0)
        high = result.get("high", 0)

        if critical > 0 or high > 0:
            send_notification(
                title=f"Customer Alert: {critical} critical, {high} high-risk",
                body=f"{len(result.get('alerts', []))} customers need your attention.",
                level="warning",
            )
    except Exception as e:
        logger.error(f"Churn check failed: {e}")
        async with async_session() as db:
            await job_guard.finish(db, run_id, status=FAILED, detail=str(e)[:500])
        return {"error": str(e)}

    async with async_session() as db:
        await job_guard.finish(
            db, run_id, status=SUCCEEDED,
            detail=f"critical={result.get('critical', 0)} high={result.get('high', 0)}",
        )
    return result
