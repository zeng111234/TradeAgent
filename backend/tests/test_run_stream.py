"""Following a run over SSE: what is sent, when, and what survives a reconnect.

The unit of this endpoint is a **round**, not a token. The loop writes progress
once per round, so a stream that pushed at any finer grain would either repeat
itself or send nothing. What these tests pin is the part a client depends on to
resume: the event id, the terminal event, and the fact that a cursor past the
end is answered rather than ignored.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.agent.loop import AgentRun, TraceStep
from app.agent.service import AgentResult


def read_events(client, url: str, *, limit: int = 50) -> list[dict]:
    """Read SSE frames until the stream closes or ``limit`` is reached."""
    events: list[dict] = []
    current = {"event": "message", "id": None, "data": None}
    with client.stream("GET", url, timeout=10) as response:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line == "":
                if current["data"] is not None:
                    events.append(current)
                    current = {"event": "message", "id": None, "data": None}
                    if len(events) >= limit:
                        break
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.lstrip(" ")
            if field == "event":
                current["event"] = value
            elif field == "id":
                current["id"] = int(value)
            elif field == "data":
                # One ``data:`` line per event, because JSON escapes newlines.
                # If that ever stops being true this parse fails loudly rather
                # than silently joining two payloads.
                current["data"] = json.loads(value)
    return events


def wait_for_terminal(client, run_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/agent/runs/{run_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


def install_run(client, monkeypatch, *, steps: int = 2, delay: float = 0.0) -> str:
    """Start a run whose whole trace is produced in-process."""
    import app.routers.agent_runs as routes

    async def fake_execute(goal, *, run_id=None, on_step=None, **kwargs):
        run = AgentRun(goal=goal, run_id=run_id)
        for index in range(steps):
            step = TraceStep(round=index + 1)
            run.steps.append(step)
            if on_step:
                on_step(step, run)
            if delay:
                await asyncio.sleep(delay)
        run.status = "succeeded"
        run.stop_reason = "final answer"
        run.final_output = "done"
        return AgentResult(
            run=run, mermaid_sequence="", mermaid_flowchart="", html=""
        )

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", fake_execute)
    response = client.post("/api/v1/agent/run", json={"goal": "stream me"})
    # 200, not 202: the route is declared without ``status_code=202`` even
    # though its own docstring promises one. Recorded rather than changed --
    # altering a success status is a breaking change (R49).
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def test_a_finished_run_sends_one_done_event_and_closes(client, monkeypatch):
    run_id = install_run(client, monkeypatch, steps=2)
    wait_for_terminal(client, run_id)

    events = read_events(client, f"/api/v1/agent/runs/{run_id}/events")

    assert [event["event"] for event in events] == ["done"]
    body = events[0]["data"]
    assert body["status"] == "succeeded"
    assert body["rounds"] == 2
    assert len(body["steps"]) == 2
    assert body["final_output"] == "done"
    # The diagram is deliberately absent from the stream: it is rebuilt from
    # the whole trace on every call and the live view does not render it.
    assert "mermaid_sequence" not in body


def test_the_done_event_carries_the_round_as_its_id(client, monkeypatch):
    run_id = install_run(client, monkeypatch, steps=3)
    wait_for_terminal(client, run_id)

    events = read_events(client, f"/api/v1/agent/runs/{run_id}/events")

    assert events[-1]["id"] == 3
    assert events[-1]["data"]["rounds"] == 3


def test_a_cursor_past_the_end_is_answered_not_ignored(client, monkeypatch):
    """A client that reconnects after the run finished has ``last_id == rounds``.

    Without the always-send-terminal rule it would wait for a round that will
    never come, on a connection that will never close.
    """
    run_id = install_run(client, monkeypatch, steps=2)
    wait_for_terminal(client, run_id)

    events = read_events(client, f"/api/v1/agent/runs/{run_id}/events?after=999")

    assert len(events) == 1
    assert events[0]["event"] == "done"


def test_progress_is_pushed_while_the_run_is_still_going(client, monkeypatch):
    """The reason to stream at all: events arrive before the run ends."""
    import app.routers.agent_runs as routes

    monkeypatch.setattr(routes, "STREAM_TICK_SECONDS", 0.02)
    run_id = install_run(client, monkeypatch, steps=3, delay=0.08)

    events = read_events(client, f"/api/v1/agent/runs/{run_id}/events")

    kinds = [event["event"] for event in events]
    assert "snapshot" in kinds, f"nothing arrived mid-run: {kinds}"
    assert kinds[-1] == "done"

    seen_rounds = [event["data"]["rounds"] for event in events if event["event"] == "snapshot"]
    assert seen_rounds == sorted(seen_rounds), "progress went backwards"
    # Not ``max < 3``: the last round can be persisted while the run is still
    # finishing, so a snapshot at the final round is legitimate. What matters is
    # that at least one arrived before the last round did.
    assert any(rounds < 3 for rounds in seen_rounds), f"only the final round was pushed: {seen_rounds}"


def test_event_ids_are_round_numbers(client, monkeypatch):
    import app.routers.agent_runs as routes

    monkeypatch.setattr(routes, "STREAM_TICK_SECONDS", 0.02)
    run_id = install_run(client, monkeypatch, steps=2, delay=0.08)

    events = read_events(client, f"/api/v1/agent/runs/{run_id}/events")

    for event in events:
        if event["event"] in ("snapshot", "done"):
            assert event["id"] == event["data"]["rounds"]


def test_an_idle_stream_sends_heartbeat_comments(client, monkeypatch):
    """A proxy closes an idle connection; a run waiting on a model is idle."""
    import app.routers.agent_runs as routes

    monkeypatch.setattr(routes, "STREAM_TICK_SECONDS", 0.02)
    monkeypatch.setattr(routes, "STREAM_HEARTBEAT_SECONDS", 0.03)
    run_id = install_run(client, monkeypatch, steps=1, delay=0.4)

    comments = 0
    with client.stream(
        "GET", f"/api/v1/agent/runs/{run_id}/events", timeout=10
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith(":"):
                comments += 1
                break

    assert comments == 1
    wait_for_terminal(client, run_id)


def test_an_unknown_run_is_a_json_404_not_an_empty_stream(client):
    """A stream that opens and then says nothing is the worst answer here."""
    response = client.get("/api/v1/agent/runs/no-such-run/events")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"
    assert "text/event-stream" not in response.headers["content-type"]


@pytest.mark.parametrize("after", ["0", "-5"])
def test_a_nonsense_cursor_starts_from_the_beginning(client, monkeypatch, after):
    run_id = install_run(client, monkeypatch, steps=1)
    wait_for_terminal(client, run_id)

    events = read_events(client, f"/api/v1/agent/runs/{run_id}/events?after={after}")

    assert events[0]["id"] == 1
