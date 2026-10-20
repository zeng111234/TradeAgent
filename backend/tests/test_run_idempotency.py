"""Retry safety for "start a run", which costs money and edits the book.

The default stays: no key, no promise. What changes is that a caller *can* ask
for one — and that a replay says so, instead of returning a run id and letting
the caller believe a second run started.

Every test mints its own key: the test database is shared, and a fixed key would
make one test's run the replay answer of the next one's.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.agent.loop import AgentRun
from app.agent.repository import start_run
from app.agent.service import AgentResult


def a_key() -> dict[str, str]:
    return {"Idempotency-Key": f"probe-{uuid.uuid4().hex}"}


def _route_to_a_fake_agent(monkeypatch):
    """Configure the route and record every execution."""
    import app.routers.agent_runs as routes

    seen: list[str] = []

    async def fake_execute(goal, *, run_id=None, **kwargs):
        seen.append(goal)
        await asyncio.sleep(0.01)
        run = AgentRun(goal=goal, run_id=run_id, status="succeeded",
                       stop_reason="final answer")
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", fake_execute)
    return seen


def test_the_same_key_starts_one_run_and_says_it_was_replayed(client, monkeypatch):
    seen = _route_to_a_fake_agent(monkeypatch)
    key = a_key()

    first = client.post("/api/v1/agent/run", json={"goal": "same goal"}, headers=key)
    second = client.post("/api/v1/agent/run", json={"goal": "same goal"}, headers=key)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["run_id"] == second.json()["run_id"]
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert seen == ["same goal"], "a retry started a second run"


def test_a_different_key_is_a_different_run(client, monkeypatch):
    seen = _route_to_a_fake_agent(monkeypatch)

    first = client.post("/api/v1/agent/run", json={"goal": "g"}, headers=a_key())
    second = client.post("/api/v1/agent/run", json={"goal": "g"}, headers=a_key())

    assert first.json()["run_id"] != second.json()["run_id"]
    assert len(seen) == 2


def test_without_a_key_nothing_is_promised(client, monkeypatch):
    """Documented, not accidental: two calls are two runs."""
    seen = _route_to_a_fake_agent(monkeypatch)

    first = client.post("/api/v1/agent/run", json={"goal": "g"})
    second = client.post("/api/v1/agent/run", json={"goal": "g"})

    assert first.json()["run_id"] != second.json()["run_id"]
    assert len(seen) == 2


def test_reusing_a_key_for_a_different_goal_is_refused(client, monkeypatch):
    seen = _route_to_a_fake_agent(monkeypatch)
    key = a_key()

    client.post("/api/v1/agent/run", json={"goal": "first goal"}, headers=key)
    response = client.post(
        "/api/v1/agent/run", json={"goal": "completely different"}, headers=key
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "idempotency_key_reuse"
    assert body["details"]["run_id"]
    assert len(seen) == 1, "the second goal was run anyway"


def test_wait_mode_is_protected_too(client, monkeypatch):
    seen = _route_to_a_fake_agent(monkeypatch)
    key = a_key()

    first = client.post(
        "/api/v1/agent/run", json={"goal": "g", "wait": True}, headers=key
    )
    second = client.post(
        "/api/v1/agent/run", json={"goal": "g", "wait": True}, headers=key
    )

    assert first.status_code == 200, first.text
    assert "steps" in first.json(), "a keyed synchronous run still returns its trace"
    assert second.status_code == 200, second.text
    # A replay cannot hand back a synchronous result it did not compute, so it
    # answers with the run it found and says so.
    assert second.json()["replayed"] is True
    assert second.json()["run_id"] == first.json()["run_id"]
    assert len(seen) == 1


def test_an_empty_key_is_refused_rather_than_ignored(client):
    response = client.post(
        "/api/v1/agent/run", json={"goal": "g"},
        headers={"Idempotency-Key": "   "},
    )

    assert response.status_code == 422, response.text
    assert "empty" in response.json()["detail"]


def test_an_overlong_key_is_refused(client):
    response = client.post(
        "/api/v1/agent/run", json={"goal": "g"},
        headers={"Idempotency-Key": "k" * 121},
    )

    assert response.status_code == 422, response.text
    assert "limit is 120" in response.json()["detail"]


async def test_the_unique_index_is_what_makes_the_race_safe(db):
    """The endpoint's IntegrityError branch is only as good as this constraint."""
    from app.agent.loop import AgentRun as AgentRunResult

    key = f"race-{uuid.uuid4().hex}"
    await start_run(db, AgentRunResult(goal="g", run_id="a" * 32), idempotency_key=key)
    with pytest.raises(IntegrityError):
        await start_run(db, AgentRunResult(goal="g", run_id="b" * 32), idempotency_key=key)
    await db.rollback()
