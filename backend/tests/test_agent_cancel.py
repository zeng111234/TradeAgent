"""Stopping a run: cooperative, recorded, and never a hard task cancel.

The failure this file exists to prevent is subtle: ``asyncio.Task.cancel()``
raises ``CancelledError``, a ``BaseException``, so the background runner's
``except Exception`` never sees it. The row stays ``running`` until the stale
sweep writes it off, and every completed round is lost. Everything here asserts
the opposite: the run finishes normally, with ``cancelled`` and its trace.
"""
from __future__ import annotations

import asyncio
import json

from app.agent import AgentBudget, AgentLoop, ToolRegistry, tool
from app.agent.loop import CancelToken
from app.agent.repository import save_run
from tests.fakes import FakeLLM, final, multi_tool_reply, tool_reply


@tool(description="Look something up")
async def lookup(keyword: str) -> dict:
    return {"keyword": keyword}


@tool(description="Take a while")
async def slow(seconds: float = 0.2) -> dict:
    await asyncio.sleep(seconds)
    return {"slept": seconds}


def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(lookup)
    reg.register(slow)
    return reg


async def test_a_stop_between_rounds_keeps_the_rounds_already_done():
    token = CancelToken()
    loop = AgentLoop(
        FakeLLM([tool_reply("lookup", {"keyword": "yarn"}), final("never reached")]),
        registry(),
        budget=AgentBudget(),
        cancel=token,
    )
    # Fires at the end of round 1, which is the boundary the loop checks.
    loop.on_step = lambda step, run: token.cancel()

    run = await loop.run("find yarn")

    assert run.status == "cancelled"
    assert run.stop_code == "cancelled"
    assert len(run.steps) == 1
    assert run.steps[0].tool_calls[0].ok is True
    assert run.final_output == ""
    assert run.tool_call_count == 1


async def test_a_stop_mid_model_call_records_what_was_already_written():
    class SlowStreamingLLM:
        model = "fake-model"
        supports_stream = True

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools, *, temperature=0.2, max_tokens=1500, on_delta=None):
            self.calls += 1
            on_delta("reasoning", "I should check the book first")
            on_delta("content", "Let me look")
            await asyncio.sleep(5)
            return final("never")

    llm = SlowStreamingLLM()
    token = CancelToken()
    loop = AgentLoop(
        llm, registry(), budget=AgentBudget(), cancel=token,
        on_delta=lambda *_: None,
    )

    task = asyncio.create_task(loop.run("find yarn"))
    await asyncio.sleep(0.05)
    token.cancel()
    run = await asyncio.wait_for(task, timeout=2)

    assert llm.calls == 1
    assert run.status == "cancelled"
    assert len(run.steps) == 1
    step = run.steps[0]
    assert step.reasoning == "I should check the book first"
    assert step.thought == "Let me look"
    assert step.note == "cancelled mid-stream"
    # Half a sentence is not an answer. The value a client renders as the
    # conclusion must stay empty.
    assert run.final_output == ""


async def test_a_tool_already_running_finishes_and_the_rest_are_refused():
    token = CancelToken()
    loop = AgentLoop(
        FakeLLM([
            multi_tool_reply(("slow", {"seconds": 0.2}), ("lookup", {"keyword": "yarn"})),
        ]),
        registry(),
        budget=AgentBudget(),
        cancel=token,
    )

    async def stop_soon():
        await asyncio.sleep(0.05)
        token.cancel()

    stopper = asyncio.create_task(stop_soon())
    run = await loop.run("call two tools")
    await stopper

    assert run.status == "cancelled"
    calls = run.steps[0].tool_calls
    assert [c.name for c in calls] == ["slow", "lookup"]
    assert calls[0].ok is True
    assert calls[0].elapsed >= 0.15, "the running tool was killed mid-flight"
    assert calls[1].ok is False
    assert calls[1].refused_reason == "cancelled"
    # A refused call did no work; the run's count must not include it.
    assert run.tool_call_count == 1


async def test_a_cancelled_run_is_persisted_as_terminal(db):
    from app.agent.loop import AgentRun, TraceStep

    run = AgentRun(goal="stop me", status="cancelled", stop_code="cancelled",
                   stop_reason="cancelled by the operator")
    run.steps.append(TraceStep(round=1, thought="partial"))
    run.elapsed = 1.2

    row = await save_run(db, run)

    assert row.status == "cancelled"
    assert row.finished_at is not None
    assert json.loads(row.trace)[0]["thought"] == "partial"


# --- the API ---------------------------------------------------------------


def test_cancelling_a_live_run_is_202_and_idempotent(client, monkeypatch):
    import app.routers.agent_runs as routes
    from app.agent.loop import AgentRun
    from app.agent.service import AgentResult

    async def fake_execute(goal, *, run_id=None, cancel=None, **kwargs):
        run = AgentRun(goal=goal, run_id=run_id)
        if cancel is not None:
            await cancel.wait()
        run.status = "cancelled"
        run.stop_code = "cancelled"
        run.stop_reason = "cancelled by the operator"
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", fake_execute)

    run_id = client.post("/api/v1/agent/run", json={"goal": "long job"}).json()["run_id"]

    first = client.post(f"/api/v1/agent/runs/{run_id}/cancel")
    second = client.post(f"/api/v1/agent/runs/{run_id}/cancel")

    assert first.status_code == 202
    assert first.json()["status"] == "cancelling"
    assert second.status_code == 202, "a second click must not be an error"

    body = wait_for(client, run_id)
    assert body["status"] == "cancelled"
    assert body["stop_code"] == "cancelled"


def test_cancelling_a_finished_run_is_409(client, monkeypatch):
    import app.routers.agent_runs as routes
    from app.agent.loop import AgentRun
    from app.agent.service import AgentResult

    async def fake_execute(goal, *, run_id=None, **kwargs):
        run = AgentRun(goal=goal, run_id=run_id, status="succeeded",
                       stop_reason="final answer", final_output="done")
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", fake_execute)

    run_id = client.post("/api/v1/agent/run", json={"goal": "quick"}).json()["run_id"]
    wait_for(client, run_id)

    response = client.post(f"/api/v1/agent/runs/{run_id}/cancel")

    assert response.status_code == 409
    assert response.json()["code"] == "run_not_cancellable"
    assert response.json()["details"]["reason"] == "finished"


def test_cancelling_an_unknown_run_is_404(client):
    response = client.post("/api/v1/agent/runs/nope/cancel")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def wait_for(client, run_id: str, *, timeout: float = 5.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/agent/runs/{run_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")
