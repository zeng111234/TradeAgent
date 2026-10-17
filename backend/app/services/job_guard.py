"""Claiming a scheduled job, so it runs once even when two things ask at once.

Before this, the guarantee was APScheduler's ``max_instances=1``: a second fire of
the *same job in the same process* was refused. That leaves three holes, and all
three are about the work rather than about the library:

1. **The refusal was silent.** A skipped morning report left a log line and no
   record, so "we decided not to run" and "nothing was ever scheduled" looked the
   same from every screen.
2. **A manual trigger bypassed it entirely.** ``POST /agent/trigger`` calls the
   job function directly; ``max_instances`` only governs APScheduler's own fires.
   Two runs then work on the same book at the same time.
3. **Another process bypassed it entirely.** Two workers, two schedulers, one
   job running twice.

The claim below closes all three with one mechanism: a ``job_runs`` row with
``status='running'``, protected by a partial unique index. The database refuses
the second one -- ``IntegrityError`` -- whoever asks and from wherever. That is
the difference between "we hope it does not overlap" and "it cannot".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job_run import (
    ABANDONED,
    RUNNING,
    SKIPPED,
    STALE_AFTER_SECONDS,
    SUCCEEDED,
    FAILED,
    JobRun,
)

logger = logging.getLogger(__name__)


async def reap_stale(db: AsyncSession, *, older_than_seconds: int = STALE_AFTER_SECONDS) -> int:
    """Resolve runs a dead process left in ``running``.

    Without this a crash blocks the job forever: the partial unique index will
    keep refusing every new claim for work that nobody is doing. Age, not a
    heartbeat, because there is nothing to heartbeat from -- the process is gone.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=older_than_seconds)
    result = await db.execute(
        update(JobRun)
        .where(JobRun.status == RUNNING, JobRun.started_at < cutoff)
        .values(
            status=ABANDONED,
            finished_at=datetime.utcnow(),
            detail="the process that claimed this job exited before finishing it",
        )
    )
    await db.commit()
    if result.rowcount:
        logger.warning("reaped %d abandoned scheduled-job run(s)", result.rowcount)
    return result.rowcount


async def try_claim(
    db: AsyncSession, job_id: str, *, trigger: str = "schedule"
) -> tuple[bool, JobRun | None, str]:
    """Claim the right to run ``job_id``. Returns ``(got_it, row, why_not)``.

    ``got_it`` is False when something else is already running this job -- which
    is recorded as a ``skipped`` row rather than dropped, because a skipped
    scheduled run is a *missing report*, and the whole complaint about the old
    behaviour was that it was invisible.
    """
    # A crashed process must not wedge the job; clear its claim first.
    await reap_stale(db)

    claim = JobRun(
        job_id=job_id,
        trigger=trigger,
        status=RUNNING,
        started_at=datetime.utcnow(),
    )
    db.add(claim)
    try:
        await db.commit()
    except IntegrityError:
        # The partial unique index did its job. This is the guarantee, not an
        # error path: the database refused a second in-flight run.
        await db.rollback()
        running = (
            await db.execute(
                select(JobRun)
                .where(JobRun.job_id == job_id, JobRun.status == RUNNING)
                .order_by(JobRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        reason = (
            f"another run of {job_id} started at "
            f"{running.started_at.isoformat() if running else 'unknown'} is still in "
            f"progress; this {trigger} attempt was skipped rather than stacked on "
            "top of it"
        )
        db.add(JobRun(
            job_id=job_id, trigger=trigger, status=SKIPPED, detail=reason,
            started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
        ))
        await db.commit()
        logger.warning("%s", reason)
        return False, None, reason

    await db.refresh(claim)
    return True, claim, ""


async def finish(
    db: AsyncSession, run_id: int, *, status: str, detail: str = ""
) -> None:
    """Record how the run ended, by id.

    By id, not by object, on purpose: the claim is committed in a short session
    and the work happens in a different one, so that a scheduled job does not hold
    a pooled connection for its whole duration. That is the same reason
    ``POST /agent/run`` lost its request-scoped session (see R17) -- with fifteen
    connections, a job holding one for minutes is a job that starves the API.
    """
    row = (
        await db.execute(select(JobRun).where(JobRun.id == run_id))
    ).scalar_one_or_none()
    if row is None:  # pragma: no cover - would mean the claim was deleted
        logger.error("job run %s disappeared before it could be finished", run_id)
        return

    row.status = status if status in (SUCCEEDED, FAILED) else FAILED
    row.detail = detail or None
    row.finished_at = datetime.utcnow()
    elapsed = row.finished_at - row.started_at
    row.elapsed_ms = int(max(0.0, elapsed.total_seconds()) * 1000)
    await db.commit()


async def recent(db: AsyncSession, job_id: str, *, limit: int = 5) -> list[dict]:
    """The last few attempts, newest first. For the status endpoint."""
    rows = (
        await db.execute(
            select(JobRun)
            .where(JobRun.job_id == job_id)
            .order_by(JobRun.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "status": row.status,
            "trigger": row.trigger,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "elapsed_ms": row.elapsed_ms,
            "detail": row.detail,
        }
        for row in rows
    ]
