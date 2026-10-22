"""Cross-conversation memory: a thread's digest, and the notes worth keeping.

The problem this solves
-----------------------
A run is a goal; a conversation is a thread of runs. Everything the model knew
about a thread reached it through ``recent_turns`` -- the last three
question/answer pairs of *that same conversation*. Open a new thread and the
assistant started from nothing: "that customer we discussed yesterday" had no
referent, and a preference stated last week had to be stated again.

What is stored, and why in two shapes
-------------------------------------
* **The digest** (``AgentConversation.summary``) is automatic. When a run
  concludes, the thread's note is rebuilt from the previous note plus what is
  new -- incremental on purpose, so the prompt does not grow with the thread and
  the cost of the fiftieth turn is the cost of the second. The model writes it,
  which means a *new* thread can be told what an old one concluded without
  anybody remembering to write it down.
* **The notes** (``agent_memories``) are deliberate. The model decides a
  sentence is worth keeping beyond any single thread and calls ``remember``. A
  person can list them, fix them, delete them.

Deliberately absent: an embedding index
---------------------------------------
"Add long-term memory" usually means a vector store over past runs. For this
book that is the wrong instrument. There are five customers; the questions are
exact ("which quotes are still open for EuroTextil"); the answers live in
structured columns that SQL matches precisely and cosine similarity matches
approximately. Retrieval would add an embedding model, an index to keep in
sync, and a class of failure -- the right passage simply not recalled -- in
exchange for nothing the digest does not already give. The digest is bounded
and inspectable: you can read exactly what the next thread will be told.

Failure is quiet and narrow
---------------------------
Summarising costs a model call, and it can fail. When it does, the thread keeps
its previous note (or none), the run is unaffected, and the next concluded run
tries again. Losing a note is acceptable; losing the run that produced it, or
letting a half-written note through, is not.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select

from app.llm import LLMError, complete_text

logger = logging.getLogger(__name__)

#: Long enough for "what was wanted, what was established, what is still owed"
#: and short enough that three of them fit in a prompt without crowding out the
#: tools. Enforced with a hard slice: a model that ignores the length instruction
#: must not be able to flood the next thread's context.
SUMMARY_MAX_CHARS = 700

#: Feeds the spend ledger. A digest is a real cost and shows up as its own line
#: rather than hiding inside an agent run's total.
SUMMARY_SCOPE = "conversation_summary"

#: How many concluded runs the digest absorbs in one rebuild. A thread that fell
#: far behind (because summarising kept failing) catches up over several runs
#: instead of sending the whole backlog to the model at once.
MAX_NEW_TURNS = 4
MAX_GOAL_CHARS = 300
MAX_ANSWER_CHARS = 900

#: How many earlier threads a new thread is told about.
CROSS_SESSION_THREADS = 3
#: How many standing notes are injected. The table is meant to stay small; this
#: is the point at which "keep everything" stops being free.
MAX_MEMORIES_IN_CONTEXT = 20

MEMORY_KEY_MAX = 80
MEMORY_VALUE_MAX = 500

_SUMMARY_SYSTEM = (
    "You keep a handover note between work sessions of a foreign-trade assistant. "
    "Given the previous note and what happened since, return the updated note and "
    "nothing else. It must say: what the operator was trying to do, what was "
    "established as fact (keep names, numbers and dates exactly as given), and what "
    "is still owed or unresolved. Drop detail that no longer matters. Never invent "
    "anything, and never turn an inference into a fact. Write in the language the "
    "material is written in. Plain prose, at most 700 characters."
)


def _session_factory():
    """Resolve the session factory lazily, so tests can repoint it."""
    from app.database import async_session

    return async_session


def _clip(text: Optional[str], limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# The digest
# --------------------------------------------------------------------------

async def _concluded_runs(db, conversation_id: str) -> list[tuple[str, str]]:
    """Every run of this thread that produced a conclusion, oldest first.

    Same rule as ``recent_turns``: an errored or stopped run has no conclusion
    to remember, and folding its empty answer into the digest would teach the
    note that the thread ended in silence.
    """
    from app.models.agent_run import AgentRun

    rows = (
        await db.execute(
            select(AgentRun.goal, AgentRun.final_output)
            .where(
                AgentRun.conversation_id == conversation_id,
                AgentRun.final_output.is_not(None),
                AgentRun.status.in_(("succeeded", "truncated")),
            )
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
        )
    ).all()
    return [(goal, output) for goal, output in rows if (output or "").strip()]


async def _compress(previous: str, turns: list[tuple[str, str]]) -> Optional[str]:
    since = []
    for index, (goal, answer) in enumerate(turns, 1):
        since.append(
            f"[{index}] asked: {_clip(goal, MAX_GOAL_CHARS)}\n"
            f"    concluded: {_clip(answer, MAX_ANSWER_CHARS)}"
        )
    prompt = (
        "PREVIOUS NOTE:\n"
        + (previous or "(none yet — this is the first note for this thread)")
        + "\n\nWHAT HAPPENED SINCE:\n"
        + "\n".join(since)
    )
    try:
        text = await complete_text(
            prompt,
            system=_SUMMARY_SYSTEM,
            temperature=0.2,
            # Sized for a reasoning model, not for the 700-character answer.
            # The configured model spends completion tokens thinking before it
            # writes anything, and at 450 the digest came back truncated every
            # time -- ``finish_reason='length'``, which ``text_of`` treats as a
            # failure, so the note was silently never written. The input is small
            # and the output is capped below, so the headroom costs little.
            max_tokens=2000,
            scope=SUMMARY_SCOPE,
        )
    except LLMError as exc:
        # A missing note is survivable; a run that failed because its bookkeeping
        # failed is not. The previous note stays, and the next run retries.
        logger.warning("could not summarise conversation: %s", exc)
        return None
    note = (text or "").strip()[:SUMMARY_MAX_CHARS]
    return note or None


async def summarise_conversation(conversation_id: Optional[str]) -> Optional[str]:
    """Rebuild one thread's digest from what is new since the last rebuild.

    Returns the note that is now stored (or the old one when there was nothing
    to add). Never raises for a model failure -- see the module docstring.

    Two short-lived sessions with the model call in between, not one session
    around the call: holding a pooled connection for the length of a completion
    is how a background task starves the request handlers on the same pool.
    """
    if not conversation_id:
        return None

    from app.models.agent_conversation import AgentConversation

    async with _session_factory()() as db:
        conversation = (
            await db.execute(
                select(AgentConversation).where(
                    AgentConversation.conversation_id == conversation_id
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            return None

        runs = await _concluded_runs(db, conversation_id)
        covered = int(conversation.summary_turns or 0)
        if len(runs) <= covered:
            return conversation.summary
        pending = runs[covered:]
        # Only the most recent ones when the thread fell behind: the tail is what
        # a follow-up depends on, and the note's job is to be current, not
        # complete.
        if len(pending) > MAX_NEW_TURNS:
            pending = pending[-MAX_NEW_TURNS:]
        previous = (conversation.summary or "").strip()

    note = await _compress(previous, pending)
    if note is None:
        return previous or None

    async with _session_factory()() as db:
        conversation = (
            await db.execute(
                select(AgentConversation).where(
                    AgentConversation.conversation_id == conversation_id
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            return note
        conversation.summary = note
        # Marked as covering the runs that were read, not the ones written since:
        # a run that concludes while this call is in flight must not be counted as
        # summarised when it was never seen.
        covered = max(covered, len(runs))
        conversation.summary_turns = covered
        conversation.summarized_at = datetime.utcnow()
        await db.commit()
    return note


# --------------------------------------------------------------------------
# What a new thread is told
# --------------------------------------------------------------------------

async def cross_session_block(conversation_id: Optional[str] = None) -> Optional[str]:
    """The context a new thread gets about the threads before it.

    Empty string is not the same as ``None`` here: ``None`` means "there is
    nothing to say", and the caller then adds no system message at all rather
    than an empty one.
    """
    from app.models.agent_conversation import AgentConversation
    from app.models.agent_memory import AgentMemory

    async with _session_factory()() as db:
        thread_stmt = select(AgentConversation).where(
            AgentConversation.summary.is_not(None)
        )
        if conversation_id:
            # A continuing thread already carries its own turns; repeating its
            # digest would tell the model what it is in the middle of doing.
            thread_stmt = thread_stmt.where(
                AgentConversation.conversation_id != conversation_id
            )
        threads = (
            await db.execute(
                thread_stmt.order_by(
                    AgentConversation.updated_at.desc(), AgentConversation.id.desc()
                ).limit(CROSS_SESSION_THREADS)
            )
        ).scalars().all()

        memories = (
            await db.execute(
                select(AgentMemory)
                .order_by(AgentMemory.updated_at.desc(), AgentMemory.id.desc())
                .limit(MAX_MEMORIES_IN_CONTEXT)
            )
        ).scalars().all()

    blocks: list[str] = []

    if threads:
        lines = [
            "Earlier threads with this operator, most recent first. These are not "
            "this conversation -- they are what you already did before it. Use them "
            "only to avoid asking for what is already known, and say when you are "
            "relying on one."
        ]
        for thread in threads:
            when = thread.updated_at.strftime("%Y-%m-%d") if thread.updated_at else "?"
            lines.append(
                f"- {when} · {thread.title or '(untitled)'}\n  {(thread.summary or '').strip()}"
            )
        blocks.append("\n".join(lines))

    if memories:
        lines = [
            "Standing notes kept across threads. They were written deliberately; "
            "follow them unless the operator says otherwise."
        ]
        for memory in memories:
            lines.append(f"- {memory.key}: {memory.value}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else None


# --------------------------------------------------------------------------
# Standing notes
# --------------------------------------------------------------------------

def _clean_key(key: str) -> str:
    return " ".join((key or "").strip().split())[:MEMORY_KEY_MAX]


async def remember(
    key: str, value: str, conversation_id: Optional[str] = None
) -> dict[str, Any]:
    """Write or overwrite one note. The key is the identity."""
    from app.models.agent_memory import AgentMemory

    clean_key = _clean_key(key)
    clean_value = (value or "").strip()[:MEMORY_VALUE_MAX]
    if not clean_key:
        return {"error": "key is required: what is this note about?"}
    if not clean_value:
        return {"error": "value is required: what should be remembered?"}

    async with _session_factory()() as db:
        existing = (
            await db.execute(
                select(AgentMemory).where(AgentMemory.key == clean_key)
            )
        ).scalar_one_or_none()
        replaced = existing is not None
        previous = existing.value if existing is not None else None
        if existing is None:
            existing = AgentMemory(key=clean_key, value=clean_value)
            db.add(existing)
        else:
            existing.value = clean_value
            existing.updated_at = datetime.utcnow()
        if conversation_id:
            existing.source_conversation_id = conversation_id
        await db.commit()

    return {
        "status": "replaced" if replaced else "remembered",
        "key": clean_key,
        "value": clean_value,
        "previous_value": previous,
        "note_count": await count_memories(),
    }


async def forget(key: str) -> dict[str, Any]:
    """Delete one note. Deleting beats archiving: a wrong note is worse than none."""
    from app.models.agent_memory import AgentMemory

    clean_key = _clean_key(key)
    if not clean_key:
        return {"error": "key is required: which note should be dropped?"}

    async with _session_factory()() as db:
        existing = (
            await db.execute(
                select(AgentMemory).where(AgentMemory.key == clean_key)
            )
        ).scalar_one_or_none()
        if existing is None:
            missing = True
        else:
            missing = False
            old_value = existing.value
            await db.delete(existing)
            await db.commit()

    if missing:
        return {"status": "not_found", "key": clean_key}

    return {
        "status": "forgotten",
        "key": clean_key,
        "value": old_value,
        "note_count": await count_memories(),
    }


async def list_memories() -> list[dict[str, Any]]:
    from app.models.agent_memory import AgentMemory

    async with _session_factory()() as db:
        rows = (
            await db.execute(
                select(AgentMemory).order_by(
                    AgentMemory.updated_at.desc(), AgentMemory.id.desc()
                )
            )
        ).scalars().all()
    return [
        {
            "key": row.key,
            "value": row.value,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]


async def count_memories() -> int:
    from sqlalchemy import func

    from app.models.agent_memory import AgentMemory

    async with _session_factory()() as db:
        return int(
            (await db.execute(select(func.count()).select_from(AgentMemory))).scalar_one()
        )
