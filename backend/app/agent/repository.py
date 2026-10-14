"""Persistence for agent runs."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import AgentRun as AgentRunResult
from app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)


async def save_run(
    db: AsyncSession,
    run: AgentRunResult,
    *,
    commit: bool = True,
) -> AgentRun:
    """Persist a finished (or failed) agent run.

    Idempotent on ``run_id``: saving the same run twice updates the existing
    row rather than creating a duplicate.
    """
    payload = run.as_dict()

    existing = (
        await db.execute(select(AgentRun).where(AgentRun.run_id == run.run_id))
    ).scalar_one_or_none()

    row = existing or AgentRun(run_id=run.run_id, goal=run.goal)
    row.goal = run.goal
    row.status = run.status
    row.stop_reason = run.stop_reason or None
    row.error = run.error
    row.final_output = run.final_output or None
    row.rounds = payload["rounds"]
    row.tool_calls = payload["tool_calls"]
    row.prompt_tokens = payload["prompt_tokens"]
    row.completion_tokens = payload["completion_tokens"]
    row.elapsed = int(payload["elapsed"] * 1000)
    row.trace = json.dumps(payload["steps"], ensure_ascii=False)
    row.finished_at = datetime.utcnow()

    if existing is None:
        db.add(row)

    if commit:
        await db.commit()
        await db.refresh(row)
    return row


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
