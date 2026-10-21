"""Persistence for agent runs."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import AgentRun as AgentRunResult
from app.models.agent_conversation import AgentConversation
from app.models.agent_run import AgentRun

logger = logging.getLogger(__name__)

#: Statuses that mean the run is over. Anything else is still a promise.
#: ``cancelled`` is terminal but not a failure: the operator stopped a run that
#: was working, and the rounds it completed are still a valid record.
TERMINAL_STATUSES = ("succeeded", "truncated", "error", "stalled", "cancelled")


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
    # save must not drop the customer, thread or model the caller named.
    if getattr(run, "customer_id", None) is not None:
        row.customer_id = run.customer_id
    if getattr(run, "conversation_id", None):
        row.conversation_id = run.conversation_id
    if getattr(run, "model", ""):
        row.model = run.model
    row.status = run.status
    # Set, never cleared, for the same reason as ``customer_id``: a progress
    # write after the terminal one must not erase the code the finish wrote.
    if getattr(run, "stop_code", ""):
        row.stop_code = run.stop_code
    row.stop_reason = run.stop_reason or None
    row.error = run.error
    row.final_output = run.final_output or None
    # Set, never cleared, for the same reason as ``customer_id``: progress writes
    # re-save the run and an empty value in a later save must not erase it.
    if getattr(run, "prompt_version", ""):
        row.prompt_version = run.prompt_version
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


async def start_run(
    db: AsyncSession,
    run: AgentRunResult,
    *,
    commit: bool = True,
    idempotency_key: Optional[str] = None,
) -> AgentRun:
    """Record that a run has started, before it does any work.

    Without this there is no row until the run finishes, so a caller polling
    ``GET /agent/runs/{run_id}`` gets 404 for the entire duration of a long
    analysis -- and the docstring promising "queue it and poll" was describing
    storage that was not actually written until the end.

    ``idempotency_key`` is stored with the row so the unique index can refuse a
    second run for the same key, including when two requests race.
    """
    run.status = "running"
    row = AgentRun(
        run_id=run.run_id, goal=run.goal, status="running",
        customer_id=getattr(run, "customer_id", None),
        conversation_id=getattr(run, "conversation_id", None),
        model=getattr(run, "model", "") or None,
        prompt_version=getattr(run, "prompt_version", "") or None,
        idempotency_key=idempotency_key,
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
        row.stop_code = "abandoned"
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


# --- conversations ---------------------------------------------------------


async def create_conversation(
    db: AsyncSession,
    *,
    title: str,
    customer_id: Optional[int] = None,
    conversation_id: Optional[str] = None,
) -> AgentConversation:
    """Open a thread. Its title is the first goal, not a model call."""
    conversation = AgentConversation(
        conversation_id=conversation_id or uuid.uuid4().hex,
        title=(title or "").strip()[:120] or "Untitled",
        customer_id=customer_id,
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def get_conversation(
    db: AsyncSession, conversation_id: str
) -> Optional[AgentConversation]:
    return (
        await db.execute(
            select(AgentConversation).where(
                AgentConversation.conversation_id == conversation_id
            )
        )
    ).scalar_one_or_none()


async def list_conversations(
    db: AsyncSession, *, limit: int = 20, offset: int = 0
) -> list[AgentConversation]:
    stmt = (
        select(AgentConversation)
        # ``id`` breaks the tie: two threads created in the same microsecond
        # would otherwise come back in an order SQLite is free to change.
        .order_by(AgentConversation.updated_at.desc(), AgentConversation.id.desc())
        .limit(max(1, min(limit, 100)))
        .offset(max(0, offset))
    )
    return list((await db.execute(stmt)).scalars().all())


async def conversation_stats(
    db: AsyncSession, conversation_ids: list[str]
) -> dict[str, dict]:
    """Run count, last status and token total, for a page of threads.

    One query for the page rather than three per thread: the listing is the
    first screen of the console and an N+1 there is paid on every visit.
    """
    if not conversation_ids:
        return {}

    rows = (
        await db.execute(
            select(
                AgentRun.conversation_id,
                AgentRun.status,
                AgentRun.goal,
                AgentRun.prompt_tokens,
                AgentRun.completion_tokens,
            )
            .where(AgentRun.conversation_id.in_(conversation_ids))
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
        )
    ).all()

    stats: dict[str, dict] = {}
    for conversation_id, status, goal, prompt, completion in rows:
        entry = stats.setdefault(
            conversation_id,
            {"run_count": 0, "last_status": None, "last_goal": "", "total_tokens": 0},
        )
        entry["run_count"] += 1
        entry["last_status"] = status
        entry["last_goal"] = goal
        entry["total_tokens"] += (prompt or 0) + (completion or 0)
    return stats


async def conversation_runs(
    db: AsyncSession, conversation_id: str, *, limit: int = 50
) -> list[AgentRun]:
    stmt = (
        select(AgentRun)
        .where(AgentRun.conversation_id == conversation_id)
        .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
        .limit(max(1, min(limit, 200)))
    )
    return list((await db.execute(stmt)).scalars().all())


async def recent_turns(
    db: AsyncSession, conversation_id: str, *, max_turns: int = 3
) -> list[tuple[str, str]]:
    """The most recent question/answer pairs a follow-up may rely on.

    Only runs that produced a conclusion: an errored or stopped run has nothing
    to be remembered by, and feeding its empty answer back would teach the model
    that the previous turn ended in silence.
    """
    stmt = (
        select(AgentRun.goal, AgentRun.final_output)
        .where(
            AgentRun.conversation_id == conversation_id,
            AgentRun.final_output.is_not(None),
            AgentRun.status.in_(("succeeded", "truncated")),
        )
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(max(1, max_turns))
    )
    rows = (await db.execute(stmt)).all()
    return [
        (goal, output)
        for goal, output in reversed(rows)
        if (output or "").strip()
    ]


async def touch_conversation(db: AsyncSession, conversation_id: str) -> None:
    """Move a thread to the top of the list after a new turn."""
    conversation = await get_conversation(db, conversation_id)
    if conversation is not None:
        conversation.updated_at = datetime.utcnow()
        await db.commit()
