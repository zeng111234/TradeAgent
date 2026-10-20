"""Scheduler - the 'brain' that runs workflows on a schedule.

Uses APScheduler to run workflows automatically:
- Every day at 22:00 UTC (06:00 Asia/Shanghai): daily morning routine
- Four times a day at 01:07/07:07/13:07/19:07 UTC: check for at-risk customers
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

#: When the churn check runs, in UTC. Four fixed hours rather than an interval.
#:
#: An ``interval`` trigger's phase is the moment the process started: deploy at
#: 14:32 and every future run moves to 20:32, 02:32, 08:32. Worse, some start
#: times put a fire exactly on 22:00 UTC -- the same minute as the morning
#: report -- and others put one at 10:00, which is the edge of the peak window
#: priced in R57: one minute later and the same work costs twice as much.
#: Neither should depend on when somebody happened to restart the service.
#:
#: 01/07/13/19 UTC is 09:00/15:00/21:00/03:00 in Asia/Shanghai: the same four
#: checks per day, at hours that are never 22 and never on the boundary.
CHURN_CHECK_HOURS = "1,7,13,19"

#: Off the hour, so this job does not queue behind everything else that fires at
#: :00, and so a log line's timestamp is enough to tell which job it was.
CHURN_CHECK_MINUTE = 7

#: Up to five minutes of spread. Two instances (a rolling deploy, a second
#: worker) would otherwise hit the same tables in the same second; the claim in
#: ``job_guard`` makes that harmless but not free.
CHURN_CHECK_JITTER_SECONDS = 300


def get_scheduler():
    """Get or create the scheduler instance."""
    global _scheduler
    if _scheduler is None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.jobstores.memory import MemoryJobStore

        _scheduler = AsyncIOScheduler(
            # Pinned, not inherited. Without this APScheduler uses the host's
            # local zone, so ``hour=22`` meant 22:00 **Asia/Shanghai** on a
            # developer's machine and 22:00 **UTC** on a server -- a six-hour
            # difference in when the daily report runs, decided by the box.
            # Measured on this repository's dev machine: the scheduler reported
            # ``Asia/Shanghai``, so the "morning" report fired at 22:00 Beijing
            # (14:00 UTC), not at 06:00 Beijing as every comment claimed.
            timezone="UTC",
            jobstores={"default": MemoryJobStore()},
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": MISFIRE_GRACE_SECONDS,
            },
        )
    return _scheduler


def register_jobs(scheduler) -> None:
    """Add every job to the scheduler, without starting it.

    Split out from :func:`start_scheduler` so the schedule itself can be
    asserted: a ``CronTrigger``'s fields and next fire time are readable on a
    scheduler that has never run, while starting one inside a unit test would
    bring the real jobs into the test session.
    """
    if scheduler.get_jobs():
        return

    # Daily morning routine at 6:00 AM (Asia/Shanghai = UTC+8, so 22:00 UTC
    # previous day). The scheduler's timezone is pinned to UTC above; without
    # that pin this comment was true only on a UTC host.
    scheduler.add_job(
        _run_daily_routine,
        "cron",
        hour=22,
        minute=0,
        id=DAILY_ROUTINE_JOB,
        name="Daily Morning Report",
        replace_existing=True,
    )

    # Churn check four times a day, on a fixed wall clock (see
    # CHURN_CHECK_HOURS for why it is not an interval).
    scheduler.add_job(
        _run_churn_check,
        "cron",
        hour=CHURN_CHECK_HOURS,
        minute=CHURN_CHECK_MINUTE,
        jitter=CHURN_CHECK_JITTER_SECONDS,
        id=CHURN_CHECK_JOB,
        name="Customer Churn Check",
        replace_existing=True,
    )


def start_scheduler():
    """Start the scheduler with all registered jobs."""
    scheduler = get_scheduler()
    register_jobs(scheduler)

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
