"""Persistence for agent runs."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import AgentRun as AgentRunResult
from app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)

#: Statuses that mean the run is over. Anything else is still a promise.
TERMINAL_STATUSES = ("succeeded", "truncated", "error", "stalled")


async def save_run(
    db: AsyncSession,
    run: AgentRunResult,
    *,
    commit: bool = True,
) -> AgentRun:
    """Persist a finished (or failed) agent run.

    Idempotent on ``run_id``: saving the same run twice updates the existing
    row rather than creating a duplicate.

    ``finished_at`` is only set for a terminal status. A row created up front so
    that a poller can see the run exists must not claim to have finished.
    """
    payload = run.as_dict()

    existing = (
        await db.execute(select(AgentRun).where(AgentRun.run_id == run.run_id))
    ).scalar_one_or_none()

    row = existing or AgentRun(
        run_id=run.run_id, goal=run.goal,
        customer_id=getattr(run, "customer_id", None),
    )
    row.goal = run.goal
    # Set, never cleared: progress writes re-save the run repeatedly, and a later
    # save must not drop the customer the caller named.
    if getattr(run, "customer_id", None) is not None:
        row.customer_id = run.customer_id
    row.status = run.status
    row.stop_reason = run.stop_reason or None
    row.error = run.error
    row.final_output = run.final_output or None
    row.rounds = payload["rounds"]
    row.tool_calls = payload["tool_calls"]
    row.prompt_tokens = payload["prompt_tokens"]
    row.completion_tokens = payload["completion_tokens"]
    # Old traces predate the field; a missing key is zero, not a KeyError.
    row.cached_prompt_tokens = payload.get("cached_prompt_tokens", 0)
    row.elapsed = int(payload["elapsed"] * 1000)
    row.trace = json.dumps(payload["steps"], ensure_ascii=False)
    if run.status in TERMINAL_STATUSES:
        row.finished_at = datetime.utcnow()

    if existing is None:
        db.add(row)

    if commit:
        await db.commit()
        await db.refresh(row)
    return row


async def start_run(db: AsyncSession, run: AgentRunResult, *, commit: bool = True) -> AgentRun:
    """Record that a run has started, before it does any work.

    Without this there is no row until the run finishes, so a caller polling
    ``GET /agent/runs/{run_id}`` gets 404 for the entire duration of a long
    analysis -- and the docstring promising "queue it and poll" was describing
    storage that was not actually written until the end.
    """
    run.status = "running"
    row = AgentRun(
        run_id=run.run_id, goal=run.goal, status="running",
        customer_id=getattr(run, "customer_id", None),
    )
    row.trace = json.dumps([], ensure_ascii=False)
    row.rounds = 0
    row.tool_calls = 0
    row.prompt_tokens = 0
    row.completion_tokens = 0
    row.cached_prompt_tokens = 0
    row.elapsed = 0
    db.add(row)
    if commit:
        await db.commit()
        await db.refresh(row)
    return row


async def mark_stale_runs(db: AsyncSession, *, older_than_seconds: int = 900) -> int:
    """Fail the runs a dead process left behind.

    A run marked ``running`` by a process that then exited can never finish, and
    a poller has no way to tell that from one that is still working. Anything
    still ``running`` after this long is not going to report, so it is failed
    with a reason instead of staying a promise forever.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=older_than_seconds)
    rows = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.status == "running", AgentRun.created_at < cutoff
            )
        )
    ).scalars().all()

    for row in rows:
        row.status = "error"
        row.error = (
            "the process that started this run exited before it finished; "
            "nothing is running it now"
        )
        row.stop_reason = "abandoned"
        row.finished_at = datetime.utcnow()
    if rows:
        await db.commit()
        logger.warning("marked %d abandoned agent run(s) as error", len(rows))
    return len(rows)


async def list_runs(
    db: AsyncSession,
    *,
    limit: int = 50,
    status: Optional[str] = None,
) -> list[AgentRun]:
    stmt = select(AgentRun).order_by(AgentRun.id.desc()).limit(max(1, min(limit, 200)))
    if status:
        stmt = stmt.where(AgentRun.status == status)
    return list((await db.execute(stmt)).scalars().all())


async def get_run(db: AsyncSession, run_id: str) -> Optional[AgentRun]:
    return (
        await db.execute(select(AgentRun).where(AgentRun.run_id == run_id))
    ).scalar_one_or_none()


def trace_of(row: AgentRun) -> list[dict]:
    """Decode the stored trace, tolerating a corrupt/empty column."""
    if not row.trace:
        return []
    try:
        value = json.loads(row.trace)
    except json.JSONDecodeError:
        logger.warning("agent run %s has an unreadable trace", row.run_id)
        return []
    return value if isinstance(value, list) else []
