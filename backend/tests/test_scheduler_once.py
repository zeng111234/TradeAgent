"""How a scheduled job is stopped from running twice -- and what "twice" missed.

Before this round the answer was APScheduler's ``max_instances=1``. That is a
real guarantee, but a narrow one, and the narrowness was invisible:

* it refuses the second fire **silently** -- a log line, no record. A skipped
  morning report and a report that was never scheduled looked identical from
  every screen a person can open;
* it governs **APScheduler's own fires only**. ``POST /agent/trigger`` calls the
  job function directly, so a human clicking "run now" during the cron produced
  two runs over the same book;
* it is **per process**. Two workers mean two schedulers and two reports.

The claim below closes all three with one mechanism, and the guarantee is the
database's rather than Python's: a partial unique index on
``job_runs (job_id) WHERE status = 'running'`` refuses the second claim from any
caller in any process. A refused attempt is *recorded* as ``skipped``, because
the complaint was never that runs overlapped -- it was that nobody could tell.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.models.job_run import (
    ABANDONED,
    FAILED,
    RUNNING,
    SKIPPED,
    SUCCEEDED,
    JobRun,
)
from app.services import job_guard




def unique_job() -> str:
    """A job id nothing else in the session uses, so the index is per test."""
    return f"test_job_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def scheduled_job(db):
    """Tidy up the fixed job ids afterwards, so the real scheduler's rows stay clean."""
    from app.scheduler import CHURN_CHECK_JOB, DAILY_ROUTINE_JOB

    yield
    async def cleanup():
        for job_id in (DAILY_ROUTINE_JOB, CHURN_CHECK_JOB):
            rows = (
                await db.execute(
                    __import__("sqlalchemy").select(JobRun).where(JobRun.job_id == job_id)
                )
            ).scalars().all()
            for row in rows:
                await db.delete(row)
        await db.commit()

    asyncio.run(cleanup())


# --------------------------------------------------------------------------
# The guarantee
# --------------------------------------------------------------------------

async def test_the_second_claim_is_refused_by_the_database(db):
    job_id = unique_job()

    got_first, first, _ = await job_guard.try_claim(db, job_id, trigger="schedule")
    got_second, second, reason = await job_guard.try_claim(db, job_id, trigger="manual")

    assert got_first is True and first is not None
    assert got_second is False and second is None
    assert "still in progress" in reason
    assert "skipped" in reason


async def test_a_refused_attempt_is_recorded_not_dropped(db):
    """The visibility half. A skipped scheduled run is a missing report."""
    job_id = unique_job()

    await job_guard.try_claim(db, job_id, trigger="schedule")
    await job_guard.try_claim(db, job_id, trigger="manual")

    history = await job_guard.recent(db, job_id)
    statuses = [entry["status"] for entry in history]
    assert RUNNING in statuses
    assert SKIPPED in statuses, (
        "the refused attempt left no record, which is the original complaint"
    )
    skipped = next(entry for entry in history if entry["status"] == SKIPPED)
    assert "still in progress" in skipped["detail"]


async def test_the_index_is_partial_and_unique(db):
    """The mechanism, asserted on the schema rather than inferred from behaviour."""
    index_sql = (
        await db.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='uq_job_runs_one_running'"
            )
        )
    ).scalar_one_or_none()

    assert index_sql is not None, "the partial unique index is gone"
    assert "UNIQUE" in index_sql.upper()
    assert "WHERE" in index_sql.upper(), (
        "without the WHERE clause the index would forbid a job ever running twice "
        "again, not just twice at once"
    )
    assert "running" in index_sql.lower()


async def test_a_finished_job_can_be_claimed_again(db):
    """The other half of "partial": history must not block the next run."""
    job_id = unique_job()

    got_first, first, _ = await job_guard.try_claim(db, job_id)
    await job_guard.finish(db, first.id, status=SUCCEEDED, detail="done")
    got_second, _, _ = await job_guard.try_claim(db, job_id)

    assert got_first and got_second, "a completed run left the job unclaimable"


# --------------------------------------------------------------------------
# Crashes
# --------------------------------------------------------------------------

async def test_a_crashed_claim_does_not_block_the_job_forever(db):
    """A dead process leaves a `running` row; without a reap the index blocks it.

    This is the failure mode a unique index introduces, so it is the one that has
    to be handled explicitly -- the same trade as ``mark_stale_runs`` for agent
    runs.
    """
    job_id = unique_job()
    got_it, claim, _ = await job_guard.try_claim(db, job_id)
    assert got_it

    # The process died: the row is old and nothing is running it.
    claim.started_at = datetime.utcnow() - timedelta(hours=3)
    await db.commit()

    got_again, _, _ = await job_guard.try_claim(db, job_id)

    assert got_again, "a crashed run wedged the job permanently"
    await db.refresh(claim)
    assert claim.status == ABANDONED
    assert "exited before finishing" in claim.detail


async def test_a_recent_running_row_is_not_reaped(db):
    job_id = unique_job()
    _, claim, _ = await job_guard.try_claim(db, job_id)

    await job_guard.reap_stale(db)

    await db.refresh(claim)
    assert claim.status == RUNNING, "a live run was reaped out from under itself"


# --------------------------------------------------------------------------
# The entry points that used to bypass the guarantee
# --------------------------------------------------------------------------

def test_a_manual_trigger_during_a_scheduled_run_is_skipped(client, db, scheduled_job, monkeypatch):
    """``max_instances`` never saw this one: the manual trigger is a direct call."""
    import app.scheduler as scheduler
    import app.workflows as workflows

    calls = []

    async def slow_routine(**_kwargs):
        calls.append("ran")
        await asyncio.sleep(0.2)
        return {"ok": True}

    monkeypatch.setattr(workflows, "daily_morning_routine", slow_routine)

    async def scenario():
        first = asyncio.create_task(scheduler._run_daily_routine(trigger="schedule"))
        await asyncio.sleep(0.05)  # let the first claim land
        second = await scheduler._run_daily_routine(trigger="manual")
        await first
        return second

    second = asyncio.run(scenario())

    assert second.get("skipped") is True, (
        "the manual trigger ran anyway; that is two routines over the same book"
    )
    assert len(calls) == 1, f"the routine ran {len(calls)} times"


def test_the_routine_records_success_with_its_timing(client, db, scheduled_job, monkeypatch):
    import app.scheduler as scheduler
    import app.workflows as workflows

    async def quick_routine(**_kwargs):
        return {"scanned": 3}

    monkeypatch.setattr(workflows, "daily_morning_routine", quick_routine)

    result = asyncio.run(scheduler._run_daily_routine(trigger="manual"))
    assert result == {"scanned": 3}

    history = asyncio.run(job_guard.recent(db, scheduler.DAILY_ROUTINE_JOB))
    assert history[0]["status"] == SUCCEEDED
    assert history[0]["trigger"] == "manual"
    assert history[0]["elapsed_ms"] is not None


def test_a_failing_routine_is_recorded_as_failed_not_left_running(
    client, db, scheduled_job, monkeypatch
):
    """A `running` row left behind would block tomorrow's run until it aged out."""
    import app.scheduler as scheduler
    import app.workflows as workflows

    async def exploding(**_kwargs):
        raise RuntimeError("the scanner fell over")

    monkeypatch.setattr(workflows, "daily_morning_routine", exploding)

    result = asyncio.run(scheduler._run_daily_routine(trigger="schedule"))

    assert "error" in result
    history = asyncio.run(job_guard.recent(db, scheduler.DAILY_ROUTINE_JOB))
    assert history[0]["status"] == FAILED
    assert "fell over" in history[0]["detail"]


# --------------------------------------------------------------------------
# The policy that APScheduler would otherwise choose for us
# --------------------------------------------------------------------------

def test_the_misfire_policy_is_stated_not_inherited():
    """APScheduler's effective default is one second, and the store is in memory.

    A restart at 22:00:02 therefore loses the day's run with nothing to recover
    it from -- the job does not exist until the code registers it again. An hour
    of grace means the run happens late and says so.
    """
    import app.scheduler as scheduler

    assert scheduler.MISFIRE_GRACE_SECONDS >= 300
    built = scheduler.get_scheduler()
    try:
        defaults = built._job_defaults
        assert defaults["misfire_grace_time"] == scheduler.MISFIRE_GRACE_SECONDS, (
            f"expected {scheduler.MISFIRE_GRACE_SECONDS}s, got "
            f"{defaults['misfire_grace_time']!r} -- a job that misses its slot by "
            "more than this is dropped, and with a memory store there is nothing "
            "left to recover it from"
        )
        assert defaults["coalesce"] is True, (
            "without coalesce a restart after several missed fires runs them all"
        )
        assert defaults["max_instances"] == 1
    finally:
        # Read off the instance without starting it: shutdown() on a scheduler
        # that never ran raises, and starting one here would bring the real cron
        # jobs into the test session.
        scheduler._scheduler = None


def test_a_scheduled_run_holds_no_pooled_connection_while_it_works(
    client, db, scheduled_job, monkeypatch
):
    """The R17 lesson, inside the scheduler.

    Claim, work and record happen in three short sessions. The first version kept
    one session open across the whole routine -- the same shape as the
    request-scoped session ``POST /agent/run`` had. Fifteen pooled connections,
    and a job holding one for minutes starves the API it shares a process with.
    """
    import app.scheduler as scheduler
    import app.workflows as workflows
    from app.database import engine

    observed = {}

    async def routine_that_looks(**_kwargs):
        observed["checked_out"] = engine.pool.checkedout()
        await asyncio.sleep(0.05)
        observed["still"] = engine.pool.checkedout()
        return {"ok": True}

    monkeypatch.setattr(workflows, "daily_morning_routine", routine_that_looks)
    asyncio.run(scheduler._run_daily_routine(trigger="manual"))

    assert observed["checked_out"] == 0, (
        "the routine is running while a pooled connection is held open"
    )
    assert observed["still"] == 0


def test_the_status_endpoint_reports_what_happened(client, db, scheduled_job, monkeypatch):
    """Where a human looks. A skipped run has to be visible here, not only in a log."""
    import app.scheduler as scheduler
    import app.workflows as workflows

    async def quick(**_kwargs):
        return {"ok": True}

    monkeypatch.setattr(workflows, "daily_morning_routine", quick)
    asyncio.run(scheduler._run_daily_routine(trigger="manual"))

    body = client.get("/api/v1/scheduler/history").json()

    assert scheduler.DAILY_ROUTINE_JOB in body
    assert body[scheduler.DAILY_ROUTINE_JOB], "no history recorded"
    assert body[scheduler.DAILY_ROUTINE_JOB][0]["status"] == SUCCEEDED


# --------------------------------------------------------------------------
# When the jobs run
# --------------------------------------------------------------------------

def _registered():
    """The scheduler with its jobs added, but never started.

    A started ``AsyncIOScheduler`` needs a running event loop, and starting one
    here would let the real cron jobs fire into the test session. Triggers are
    readable without any of that.
    """
    import app.scheduler as scheduler

    built = scheduler.get_scheduler()
    scheduler.register_jobs(built)
    return built, {job.id: job for job in built.get_jobs()}


def _next_fires(trigger, start, count):
    fires, previous, now = [], None, start
    for _ in range(count):
        nxt = trigger.get_next_fire_time(previous, now)
        fires.append(nxt)
        previous, now = nxt, nxt
    return fires


def test_the_scheduler_runs_on_utc_not_on_the_hosts_clock():
    """Measured before this was pinned: the scheduler reported ``Asia/Shanghai``.

    ``hour=22`` therefore meant 22:00 Beijing on this machine and 22:00 UTC on a
    server -- the "morning" report landing at 10pm local for anyone developing
    outside UTC. The intent was always 06:00 Asia/Shanghai, which is 22:00 UTC;
    pinning the timezone is what makes the comment true everywhere.
    """
    from datetime import datetime, timezone

    import app.scheduler as scheduler

    built, jobs = _registered()
    try:
        assert str(built.timezone) == "UTC", (
            f"the scheduler follows the host clock ({built.timezone}); every cron "
            "hour in this file would mean something different on each machine"
        )

        daily = jobs[scheduler.DAILY_ROUTINE_JOB]
        # From 12:00 UTC the next morning report is 22:00 UTC the same day.
        nxt = daily.trigger.get_next_fire_time(
            None, datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        )
        assert (nxt.hour, nxt.minute, nxt.utcoffset().total_seconds()) == (22, 0, 0), (
            f"the morning routine fires at {nxt} -- 22:00 UTC is 06:00 in Shanghai"
        )
    finally:
        scheduler._scheduler = None


def test_the_churn_check_runs_on_four_fixed_hours_not_an_interval():
    """An interval's phase is the moment the process started.

    Deploy at 14:32 and every future run moves with it; some start times put a
    fire on 22:00 UTC -- the same minute as the morning report -- and others on
    the edge of the peak window priced in R57. Fixed hours are the same work
    with a phase that does not depend on when somebody restarted the service.
    """
    from datetime import datetime, timezone

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    import app.scheduler as scheduler

    built, jobs = _registered()
    try:
        churn = jobs[scheduler.CHURN_CHECK_JOB]

        assert isinstance(churn.trigger, CronTrigger), f"still {type(churn.trigger).__name__}"
        assert not isinstance(churn.trigger, IntervalTrigger)

        fields = {field.name: str(field) for field in churn.trigger.fields}
        assert fields["hour"] == "1,7,13,19"
        assert fields["minute"] == "7"

        fires = _next_fires(
            churn.trigger, datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc), 8
        )
        hours = [fire.hour for fire in fires]
        assert set(hours) <= {1, 7, 13, 19}, hours
        assert 22 not in hours, "a churn check is scheduled on top of the morning report"
        # Jitter may move the minute, never the hour.
        assert all(fire.minute in range(7, 13) for fire in fires), fires
    finally:
        scheduler._scheduler = None


def test_the_two_jobs_never_share_a_slot():
    """The property that mattered: no churn check at the same time as the report."""
    import app.scheduler as scheduler

    built, jobs = _registered()
    try:
        def hours(job):
            field = next(f for f in job.trigger.fields if f.name == "hour")
            return set(str(field).split(","))

        daily = hours(jobs[scheduler.DAILY_ROUTINE_JOB])
        churn = hours(jobs[scheduler.CHURN_CHECK_JOB])

        assert daily == {"22"}, daily
        assert churn == {"1", "7", "13", "19"}, churn
        assert not (daily & churn)
    finally:
        scheduler._scheduler = None
