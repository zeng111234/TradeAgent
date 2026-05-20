"""Scheduler - the 'brain' that runs workflows on a schedule.

Uses APScheduler to run workflows automatically:
- Every day at 9:00 AM: daily morning routine
- Every 6 hours: check for at-risk customers
- On-demand: manual trigger via API

The scheduler starts with the FastAPI app and runs in the background.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler = None


def get_scheduler():
    """Get or create the scheduler instance."""
    global _scheduler
    if _scheduler is None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.jobstores.memory import MemoryJobStore

        _scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            job_defaults={"coalesce": True, "max_instances": 1},
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
            id="daily_morning_routine",
            name="Daily Morning Report",
            replace_existing=True,
        )

        # Churn check every 6 hours
        scheduler.add_job(
            _run_churn_check,
            "interval",
            hours=6,
            id="churn_check",
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
    """Get status of all scheduled jobs."""
    scheduler = get_scheduler()
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else "Not scheduled",
            "trigger": str(job.trigger),
        })
    return jobs


async def trigger_daily_routine(search_keywords: str = "textile buyer", target_country: str = ""):
    """Manually trigger the daily routine (for testing or on-demand use)."""
    logger.info("Manual trigger: daily routine")
    return await _run_daily_routine(search_keywords=search_keywords, target_country=target_country)


async def _run_daily_routine(search_keywords: str = "textile buyer", target_country: str = ""):
    """Internal function to run the daily routine."""
    from app.workflows import daily_morning_routine
    try:
        result = await daily_morning_routine(
            search_keywords=search_keywords,
            target_country=target_country,
        )
        return result
    except Exception as e:
        logger.error(f"Daily routine failed: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


async def _run_churn_check():
    """Internal function to run churn check."""
    try:
        from app.database import async_session
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