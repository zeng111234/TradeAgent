"""Cross-conversation memory.

The point of these tests is behavioural, not structural: after this change a
*new* thread must know what an old one concluded, and a note the model wrote must
be readable and removable by a person. Every test therefore goes through the
public functions the runtime uses, and asserts on what a later thread would
actually be told.
"""
from __future__ import annotations

import uuid

import pytest

from app.agent import memory as memory_mod
from app.agent.memory import (
    cross_session_block,
    forget,
    list_memories,
    remember,
    summarise_conversation,
)
from app.models.agent_conversation import AgentConversation
from app.models.agent_memory import AgentMemory
from app.models.agent_run import AgentRun

from sqlalchemy import select


def _key() -> str:
    return f"pytest note {uuid.uuid4().hex[:8]}"


async def _purge(db, *, keys=(), conversation_ids=()) -> None:
    for key in keys:
        row = (
            await db.execute(select(AgentMemory).where(AgentMemory.key == key))
        ).scalar_one_or_none()
        if row is not None:
            await db.delete(row)
    for cid in conversation_ids:
        for model in (AgentRun, AgentConversation):
            rows = (
                await db.execute(
                    select(model).where(model.conversation_id == cid)
                )
            ).scalars().all()
            for row in rows:
                await db.delete(row)
    await db.commit()


async def _make_thread(
    db, answers, *, status="succeeded", summary=None, summary_turns=None
) -> str:
    """A conversation with one concluded run per answer, oldest first.

    ``summary`` is written as-is when given, so a test can hand the thread a note
    that predates its runs. ``summary_turns`` defaults to "the note covers
    everything already", which is the state after a successful rebuild.
    """
    cid = uuid.uuid4().hex
    row = AgentConversation(conversation_id=cid, title=f"pytest thread {cid[:6]}")
    if summary is not None:
        row.summary = summary
        row.summary_turns = len(answers) if summary_turns is None else summary_turns
    db.add(row)
    for index, answer in enumerate(answers, 1):
        db.add(
            AgentRun(
                run_id=uuid.uuid4().hex,
                goal=f"pytest goal {index}: count the customers",
                final_output=answer,
                status=status,
                conversation_id=cid,
            )
        )
    await db.commit()
    return cid


# --------------------------------------------------------------- the notes ---

async def test_a_note_written_is_a_note_read_back(db):
    key = _key()
    try:
        result = await remember(key, "quotes go out in USD, FOB Ningbo")
        assert result["status"] == "remembered"
        assert result["key"] == key

        rows = await list_memories()
        assert any(r["key"] == key and "FOB Ningbo" in r["value"] for r in rows)
    finally:
        await _purge(db, keys=[key])


async def test_the_same_key_replaces_instead_of_piling_up(db):
    key = _key()
    try:
        await remember(key, "quotes in USD")
        before = len(await list_memories())
        second = await remember(key, "quotes in EUR")

        assert second["status"] == "replaced", "a repeat key must update, not duplicate"
        assert second["previous_value"] == "quotes in USD"
        after = await list_memories()
        assert len(after) == before, "the note count must not grow"
        assert [r["value"] for r in after if r["key"] == key] == ["quotes in EUR"]
    finally:
        await _purge(db, keys=[key])


async def test_an_empty_note_is_refused_rather_than_stored(db):
    assert "error" in await remember("", "something")
    assert "error" in await remember(_key(), "   ")


async def test_forget_removes_the_note_and_says_so(db):
    key = _key()
    await remember(key, "temp")
    try:
        result = await forget(key)
        assert result["status"] == "forgotten"
        assert result["value"] == "temp"
        assert not any(r["key"] == key for r in await list_memories())

        again = await forget(key)
        assert again["status"] == "not_found", "deleting twice must not pretend to succeed"
    finally:
        await _purge(db, keys=[key])


# ------------------------------------------------- what a new thread sees ---

async def test_nothing_known_means_no_block_at_all(db):
    """``None``, not ``""``: the caller then adds no system message at all.

    Empties the shared test database's memory on purpose -- no other test in the
    suite depends on a digest or a note existing before it runs.
    """
    for row in (await db.execute(select(AgentConversation))).scalars().all():
        if row.summary:
            row.summary = None
    for row in (await db.execute(select(AgentMemory))).scalars().all():
        await db.delete(row)
    await db.commit()

    assert await cross_session_block(None) is None


async def test_an_earlier_thread_is_visible_from_a_new_one(db):
    cid = await _make_thread(
        db,
        ["CRM holds 5 customers"],
        summary="CRM holds 5 customers; 3 tasks are open",
    )
    try:
        block = await cross_session_block("a-brand-new-conversation")
        assert block is not None
        assert "5 customers" in block, "the earlier conclusion must reach the new thread"
        assert "pytest thread" in block, "the note must be attributed to its thread"
    finally:
        await _purge(db, conversation_ids=[cid])


async def test_a_thread_is_not_told_its_own_digest(db):
    """A continuing thread already carries its turns; repeating them is noise.

    Without the exclusion the model is handed its own summary *and* its turns,
    which reads as two different threads reaching the same conclusion.
    """
    cid = await _make_thread(
        db, ["eleven customers in the funnel"], summary="eleven customers in the funnel"
    )
    try:
        own = await cross_session_block(cid)
        assert own is None or "eleven customers" not in own

        stranger = await cross_session_block("some-other-thread")
        assert stranger is not None and "eleven customers" in stranger
    finally:
        await _purge(db, conversation_ids=[cid])


async def test_standing_notes_are_included_in_the_block(db):
    key = _key()
    await remember(key, "never call before 10:00 their time")
    try:
        block = await cross_session_block(None)
        assert block is not None
        assert key in block
        assert "never call before 10:00" in block
    finally:
        await _purge(db, keys=[key])


# ------------------------------------------------------------- the digest ---

def _fake_model(monkeypatch, reply="A note about what happened.", fail=False):
    calls = []

    async def fake(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        if fail:
            from app.llm import LLMTransient

            raise LLMTransient("provider down")
        return reply

    monkeypatch.setattr(memory_mod, "complete_text", fake)
    return calls


async def test_a_concluded_run_becomes_a_digest(db, monkeypatch):
    calls = _fake_model(monkeypatch, "Five customers, three open tasks, EuroTextil quoted.")
    cid = await _make_thread(db, ["Five customers, three open tasks."])
    try:
        note = await summarise_conversation(cid)
        assert note == "Five customers, three open tasks, EuroTextil quoted."
        assert len(calls) == 1, "one model call for one new turn"

        # And it is stored where the next thread will find it.
        stored = (
            await db.execute(
                select(AgentConversation).where(AgentConversation.conversation_id == cid)
            )
        ).scalar_one()
        await db.refresh(stored)
        assert stored.summary == note
        assert stored.summary_turns == 1
        assert stored.summarized_at is not None
    finally:
        await _purge(db, conversation_ids=[cid])


async def test_a_run_without_a_conclusion_is_not_summarised(db, monkeypatch):
    """An errored run has nothing to remember, and folding its silence in teaches
    the note that the thread ended with no answer."""
    calls = _fake_model(monkeypatch)
    cid = await _make_thread(db, [""], status="error")
    try:
        note = await summarise_conversation(cid)
        assert note is None
        assert calls == [], "a run with no conclusion must not cost a model call"
    finally:
        await _purge(db, conversation_ids=[cid])


async def test_the_old_note_survives_a_model_failure(db, monkeypatch):
    _fake_model(monkeypatch, fail=True)
    # summary_turns=0: the note predates the run, so there IS something new to
    # fold in and the failure path is the one being exercised.
    cid = await _make_thread(
        db, ["one customer"], summary="previous, still true", summary_turns=0
    )
    try:
        note = await summarise_conversation(cid)
        assert note == "previous, still true", "a failed rebuild must not erase the note"

        stored = (
            await db.execute(
                select(AgentConversation).where(AgentConversation.conversation_id == cid)
            )
        ).scalar_one()
        await db.refresh(stored)
        assert stored.summary == "previous, still true"
        assert stored.summary_turns == 0, "an unread turn must not be marked as covered"
    finally:
        await _purge(db, conversation_ids=[cid])


async def test_the_same_turns_are_not_summarised_twice(db, monkeypatch):
    """Cost note: the digest is rebuilt incrementally, so the fiftieth turn does
    not re-read the first forty-nine. Without this the second call would send the
    whole thread again and the price would grow with the conversation."""
    calls = _fake_model(monkeypatch, "note one")
    cid = await _make_thread(db, ["first conclusion"])
    try:
        await summarise_conversation(cid)
        assert len(calls) == 1

        again = await summarise_conversation(cid)
        assert len(calls) == 1, "nothing new means no second model call"
        assert again == "note one"
    finally:
        await _purge(db, conversation_ids=[cid])


async def test_only_what_is_new_is_sent_to_the_model(db, monkeypatch):
    calls = _fake_model(monkeypatch, "rolls forward")
    cid = await _make_thread(db, ["first conclusion"])
    try:
        await summarise_conversation(cid)
        first_prompt = calls[0]["prompt"]
        assert "first conclusion" in first_prompt

        # A second turn arrives; the rebuild must carry the old note and only
        # the new turn.
        db.add(
            AgentRun(
                run_id=uuid.uuid4().hex,
                goal="second goal",
                final_output="second conclusion",
                status="succeeded",
                conversation_id=cid,
            )
        )
        await db.commit()

        await summarise_conversation(cid)
        second_prompt = calls[1]["prompt"]
        assert "second conclusion" in second_prompt
        assert "rolls forward" in second_prompt, "the previous note must be carried"
        assert "first conclusion" not in second_prompt, (
            "an already-summarised turn must not be sent again"
        )
    finally:
        await _purge(db, conversation_ids=[cid])


# --------------------------------------------------- into the actual prompt ---

async def test_the_loop_puts_memory_in_its_own_system_message():
    """Not folded into the instructions: what an earlier thread concluded is
    background for this goal, and the fingerprint of the prompt must not move
    because somebody's book changed."""
    from app.agent.loop import DEFAULT_SYSTEM_PROMPT, run_agent
    from app.agent.tools import ToolRegistry

    from scripted_llm import ScriptedLLM

    llm = ScriptedLLM([{"content": "done"}])
    run = await run_agent(
        "count the customers", llm, ToolRegistry(), memory="EARLIER THREAD: five customers"
    )

    messages = llm.calls[0].messages
    assert messages[0].role == "system"
    assert messages[0].content == DEFAULT_SYSTEM_PROMPT, "the instructions must not change"
    assert messages[1].role == "system"
    assert "EARLIER THREAD" in messages[1].content
    assert run.memory_chars == len("EARLIER THREAD: five customers")


async def test_no_memory_means_no_extra_message():
    """An empty block must not become an empty system message the model tries to
    interpret."""
    from app.agent.loop import run_agent
    from app.agent.tools import ToolRegistry

    from scripted_llm import ScriptedLLM

    llm = ScriptedLLM([{"content": "done"}])
    run = await run_agent("count the customers", llm, ToolRegistry())

    assert len(llm.calls[0].messages) == 2, "system prompt + the goal, nothing else"
    assert run.memory_chars == 0
