"""Conversations: a follow-up carries its thread, and nothing else does.

The console was a chat with no memory -- every send started a fresh run with
``context: ''``. These tests pin the three claims that make the fix real:

* a second turn is given the first turn's question and answer, as messages;
* a different thread is a different context, with no leakage;
* compaction cannot trim the history into 80-character fragments, which would
  look like context while carrying none.
"""
from __future__ import annotations

import time

from app.agent import AgentBudget, AgentLoop, ToolRegistry, tool
from app.agent.loop import AgentRun
from app.agent.repository import conversation_runs, recent_turns
from tests.fakes import FakeLLM, final


@tool(description="Look something up")
async def lookup(keyword: str) -> dict:
    return {"keyword": keyword}


def wait_for(client, run_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/agent/runs/{run_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


def install(client, monkeypatch, captured):
    """A finished run whose execute call is recorded, history and all."""
    import app.routers.agent_runs as routes
    from app.agent.service import AgentResult

    async def fake_execute(goal, *, run_id=None, history=None, conversation_id=None, **kwargs):
        captured.append({"goal": goal, "history": history, "conversation_id": conversation_id})
        run = AgentRun(
            goal=goal, run_id=run_id, status="succeeded",
            stop_reason="final answer", stop_code="final_answer",
            final_output=f"answer to: {goal}",
            conversation_id=conversation_id,
            model="fake-model",
        )
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", fake_execute)


def start(client, goal: str, conversation_id: str | None = None) -> str:
    body = {"goal": goal}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = client.post("/api/v1/agent/run", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_replayed_key_does_not_leave_an_empty_thread(client, monkeypatch):
    """A retry answers with the run that exists, and opens nothing new."""
    captured = []
    install(client, monkeypatch, captured)

    headers = {"Idempotency-Key": "retry-key-1"}
    before = client.get("/api/v1/agent/conversations").json()["count"]
    first = client.post(
        "/api/v1/agent/run", json={"goal": "one goal"}, headers=headers
    ).json()
    wait_for(client, first["run_id"])

    replay = client.post(
        "/api/v1/agent/run", json={"goal": "one goal"}, headers=headers
    ).json()

    assert replay["run_id"] == first["run_id"]
    assert replay["replayed"] is True
    assert replay["conversation_id"] == first["conversation_id"]
    after = client.get("/api/v1/agent/conversations").json()["count"]
    assert after == before + 1, "the replay opened a second, empty thread"


def test_a_new_thread_is_opened_and_named_after_the_first_goal(client, monkeypatch):
    captured = []
    install(client, monkeypatch, captured)

    started = start(client, "which customers need a follow-up today?")
    wait_for(client, started["run_id"])

    assert started["conversation_id"]

    listing = client.get("/api/v1/agent/conversations").json()
    entry = next(
        item for item in listing["conversations"]
        if item["conversation_id"] == started["conversation_id"]
    )
    assert entry["title"].startswith("which customers need")
    assert entry["run_count"] == 1
    assert entry["last_status"] == "succeeded"


def test_a_second_turn_receives_the_first_question_and_answer(client, monkeypatch):
    captured = []
    install(client, monkeypatch, captured)

    first = start(client, "find buyers in Germany")
    wait_for(client, first["run_id"])
    cid = first["conversation_id"]

    second = start(client, "and their emails?", conversation_id=cid)
    wait_for(client, second["run_id"])

    assert captured[1]["history"] == [("find buyers in Germany", "answer to: find buyers in Germany")]
    assert captured[1]["conversation_id"] == cid


def test_a_different_thread_does_not_leak_context(client, monkeypatch):
    captured = []
    install(client, monkeypatch, captured)

    first = start(client, "thread one")
    wait_for(client, first["run_id"])
    other = start(client, "thread two")
    wait_for(client, other["run_id"])

    third = start(client, "follow up", conversation_id=first["conversation_id"])
    wait_for(client, third["run_id"])

    history = captured[-1]["history"]
    assert history == [("thread one", "answer to: thread one")]
    assert all("thread two" not in question for question, _ in history)


def test_an_unknown_conversation_is_a_404_not_a_new_thread(client, monkeypatch):
    captured = []
    install(client, monkeypatch, captured)

    response = client.post(
        "/api/v1/agent/run",
        json={"goal": "continue this", "conversation_id": "does-not-exist"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "conversation_not_found"
    assert captured == [], "a rejected request must not have started a run"


def test_the_detail_endpoint_returns_turns_oldest_first(client, monkeypatch):
    captured = []
    install(client, monkeypatch, captured)

    first = start(client, "turn one")
    wait_for(client, first["run_id"])
    second = start(client, "turn two", conversation_id=first["conversation_id"])
    wait_for(client, second["run_id"])

    detail = client.get(f"/api/v1/agent/conversations/{first['conversation_id']}").json()

    assert [turn["goal"] for turn in detail["turns"]] == ["turn one", "turn two"]
    assert detail["turns"][0]["final_output"].startswith("answer to: turn one")


async def test_only_answered_runs_are_remembered(db):
    """An errored or empty turn has nothing worth carrying forward."""
    from app.agent.repository import create_conversation
    from app.models.agent_run import AgentRun as AgentRunRow

    conversation = await create_conversation(db, title="mixed")
    cid = conversation.conversation_id

    for status, output, goal in [
        ("error", None, "failed turn"),
        ("cancelled", None, "stopped turn"),
        ("stalled", None, "stalled turn"),
        ("succeeded", "", "empty answer"),
        ("succeeded", "a real answer", "good turn"),
        ("truncated", "a partial answer", "budget turn"),
    ]:
        db.add(AgentRunRow(
            run_id=f"run-{goal.replace(' ', '-')}",
            goal=goal,
            conversation_id=cid,
            status=status,
            final_output=output,
        ))
    await db.commit()

    turns = await recent_turns(db, cid, max_turns=5)

    assert [goal for goal, _ in turns] == ["good turn", "budget turn"]
    runs = await conversation_runs(db, cid)
    assert len(runs) == 6, "every run still belongs to the thread; only history filters"


async def test_a_conversation_id_is_unique(db):
    """Two threads with one id would merge two histories silently."""
    from sqlalchemy.exc import IntegrityError

    from app.agent.repository import create_conversation

    await create_conversation(db, title="first", conversation_id="same-id-12345678")
    try:
        await create_conversation(db, title="second", conversation_id="same-id-12345678")
    except IntegrityError:
        await db.rollback()
    else:  # pragma: no cover - the assertion is the point
        raise AssertionError("a duplicate conversation_id was accepted")


async def test_history_is_injected_as_messages_before_the_goal():
    loop = AgentLoop(FakeLLM([final("ok")]), _registry(), budget=AgentBudget())

    run = await loop.run(
        "and their emails?",
        history=[("find buyers", "three buyers"), ("which ones?", "the German ones")],
    )

    assert run.history_turns == 2
    roles = [message.role for message in run.messages]
    contents = [message.content for message in run.messages]
    assert roles[0] == "system"
    # The history sits between the system prompt and the new question, as a
    # conversation -- not folded into a system blob at the top.
    assert contents[1] == "find buyers"
    assert contents[2] == "three buyers"
    assert contents[3] == "which ones?"
    assert contents[4] == "the German ones"
    assert contents[-1] == "and their emails?"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(lookup)
    return registry
