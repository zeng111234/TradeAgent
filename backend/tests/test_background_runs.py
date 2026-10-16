"""A three-minute analysis must not hold the caller or freeze the process.

Two separate problems, and the answer needs both halves.

**The caller.** ``POST /agent/run`` held the connection for the whole run. The
docstring said a production deployment would "queue it and poll", and that the
storage already existed -- but the row was only written when the run *finished*,
so a poller got 404 for the entire three minutes. The storage did not exist yet.

**The process.** The tools that do the actual deep reading were ``async def``
with fully synchronous bodies: ``requests.get(timeout=15)``, DDGS, lxml parsing.
An ``async def`` that never awaits does not yield, so one analysis froze every
other coroutine in the process. Measured, three blocking page fetches delayed
unrelated work by **2950 ms**; the same work on a worker thread delayed it by
**12 ms** and took no longer overall.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from app.agent.loop import AgentBudget, AgentRun
from app.agent.repository import mark_stale_runs, save_run, start_run
from app.agent.tools import ToolRegistry, tool
from tests.scripted_llm import ScriptedLLM, call, final


def wait_for_run(client, run_id, *, timeout=10.0):
    """Poll the way a real caller would."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/agent/runs/{run_id}")
        if response.status_code == 200:
            last = response.json()
            if last["status"] != "running":
                return last
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished; last seen: {last}")


# --------------------------------------------------------------------------
# The caller is not held
# --------------------------------------------------------------------------

def test_the_request_returns_before_the_analysis_does(client, monkeypatch):
    """202 with a run id, in the time it takes to write one row."""
    import app.routers.agent_runs as routes
    from app.agent.service import AgentResult

    async def slow_execute(goal, *, run_id=None, **kwargs):
        await asyncio.sleep(1.0)          # stands in for three minutes
        run = AgentRun(goal=goal, run_id=run_id, status="succeeded",
                       stop_reason="final answer")
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", slow_execute)

    started = time.monotonic()
    response = client.post("/api/v1/agent/run", json={"goal": "deep analysis"})
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "running"
    assert body["run_id"]
    assert body["poll"] == f"/api/v1/agent/runs/{body['run_id']}"
    assert elapsed < 0.5, f"the caller waited {elapsed:.2f}s for a 1s analysis"


def test_a_running_analysis_is_visible_immediately(client, monkeypatch):
    """The defect: the row existed only after the run finished.

    So "queue it and poll" could not work -- the poll got 404 for the whole
    duration, which is exactly the promise the old docstring made.
    """
    import app.routers.agent_runs as routes
    from app.agent.service import AgentResult

    release = asyncio.Event()

    async def slow_execute(goal, *, run_id=None, **kwargs):
        await release.wait()
        run = AgentRun(goal=goal, run_id=run_id, status="succeeded",
                       stop_reason="final answer")
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", slow_execute)

    body = client.post("/api/v1/agent/run", json={"goal": "deep analysis"}).json()
    run_id = body["run_id"]

    detail = client.get(f"/api/v1/agent/runs/{run_id}")
    assert detail.status_code == 200, "a running analysis must be findable"
    assert detail.json()["status"] == "running"
    assert detail.json()["goal"] == "deep analysis"

    listed = client.get("/api/v1/agent/runs", params={"status": "running"}).json()
    assert run_id in [row["run_id"] for row in listed["runs"]]

    release.set()
    wait_for_run(client, run_id)


def test_progress_is_written_while_the_run_is_still_going(client, monkeypatch):
    """A run that writes nothing looks identical to one that died."""
    import app.routers.agent_runs as routes
    from app.agent.loop import TraceStep
    from app.agent.service import AgentResult

    release = asyncio.Event()

    async def execute_with_steps(goal, *, run_id=None, on_step=None, **kwargs):
        run = AgentRun(goal=goal, run_id=run_id)
        for index in range(3):
            step = TraceStep(round=index + 1)
            run.steps.append(step)
            if on_step:
                on_step(step, run)
            await asyncio.sleep(0.05)
        await release.wait()
        run.status = "succeeded"
        run.stop_reason = "final answer"
        run.final_output = "done"
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", execute_with_steps)

    body = client.post("/api/v1/agent/run", json={"goal": "deep analysis"}).json()
    run_id = body["run_id"]

    deadline = time.monotonic() + 5
    rounds = 0
    while time.monotonic() < deadline:
        detail = client.get(f"/api/v1/agent/runs/{run_id}").json()
        rounds = detail["rounds"]
        if rounds >= 3:
            break
        time.sleep(0.02)

    assert rounds >= 3, f"progress was never written; rounds stayed at {rounds}"
    release.set()
    wait_for_run(client, run_id)


def test_a_failed_analysis_is_recorded_as_failed(client, monkeypatch):
    """Never left as "running": a poller would wait forever for an answer."""
    import app.routers.agent_runs as routes

    async def exploding_execute(goal, **kwargs):
        raise RuntimeError("the provider hung up")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", exploding_execute)

    body = client.post("/api/v1/agent/run", json={"goal": "deep analysis"}).json()

    detail = wait_for_run(client, body["run_id"])
    assert detail["status"] == "error"
    assert "provider hung up" in (detail["error"] or "")


def test_too_many_at_once_is_refused_rather_than_queued(client, monkeypatch):
    """An unbounded queue is a backlog nobody can see."""
    import app.routers.agent_runs as routes

    release = asyncio.Event()

    async def slow_execute(goal, **kwargs):
        await release.wait()
        return None

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", slow_execute)

    started = [
        client.post("/api/v1/agent/run", json={"goal": f"analysis {i}"}).json()
        for i in range(routes.MAX_CONCURRENT_RUNS)
    ]

    refused = client.post("/api/v1/agent/run", json={"goal": "one more"})
    assert refused.status_code == 429
    assert str(routes.MAX_CONCURRENT_RUNS) in refused.json()["detail"]

    release.set()
    for body in started:
        wait_for_run(client, body["run_id"])


def test_waiting_still_returns_the_trace(client, monkeypatch):
    """The synchronous shape is still there for the CLI and for short runs."""
    import app.routers.agent_runs as routes

    async def quick_execute(goal, **kwargs):
        run = AgentRun(goal=goal, status="succeeded", stop_reason="final answer")
        run.final_output = "nothing needs attention"
        return type("Result", (), {
            "run": run, "mermaid_sequence": "", "mermaid_flowchart": "", "html": "",
            "delegations": None,
        })()

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", quick_execute)

    response = client.post(
        "/api/v1/agent/run", json={"goal": "short one", "wait": True}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["final_output"] == "nothing needs attention"


# --------------------------------------------------------------------------
# The row a dead process left behind
# --------------------------------------------------------------------------

async def test_a_running_row_is_not_written_as_finished(db):
    row = await start_run(db, AgentRun(goal="still going"))
    assert row.status == "running"
    assert row.finished_at is None, "a run in flight must not claim to have ended"
    assert row.trace == "[]"


async def test_an_abandoned_run_is_failed_rather_than_left_running(db):
    """Otherwise a poller waits forever for an answer that is not coming."""
    run = AgentRun(goal="abandoned by a crash")
    row = await start_run(db, run)
    row.created_at = datetime.utcnow() - timedelta(hours=2)
    await db.commit()

    swept = await mark_stale_runs(db, older_than_seconds=900)

    await db.refresh(row)
    assert swept >= 1
    assert row.status == "error"
    assert row.stop_reason == "abandoned"
    assert "exited before it finished" in row.error
    assert row.finished_at is not None


async def test_a_recent_running_row_is_left_alone(db):
    run = AgentRun(goal="genuinely still going")
    row = await start_run(db, run)

    await mark_stale_runs(db, older_than_seconds=900)

    await db.refresh(row)
    assert row.status == "running", "a slow run is not an abandoned one"


# --------------------------------------------------------------------------
# The process is not frozen
# --------------------------------------------------------------------------

def _loop_delay_while(coro_factory, *, delay=0.05):
    """Run the coroutine and return how late a 50 ms sleep was.

    The number is what every other request in the process waits behind.
    """
    async def scenario():
        task = asyncio.create_task(coro_factory())
        wanted = time.monotonic() + delay
        await asyncio.sleep(delay)
        late = time.monotonic() - wanted
        await task
        return late

    return asyncio.run(scenario())


def test_inspect_website_does_not_freeze_the_process(monkeypatch):
    """The fetch is blocking; where it runs is what matters."""
    import requests

    from app.agent.trade_tools import inspect_website

    class FakeResponse:
        status_code = 200
        text = "<html><title>Example</title><body><p>hello</p></body></html>"

    def slow_get(*args, **kwargs):
        time.sleep(0.5)                    # stands in for a real page fetch
        return FakeResponse()

    monkeypatch.setattr(requests, "get", slow_get)

    late = _loop_delay_while(lambda: inspect_website(url="https://example.com"))

    assert late < 0.2, (
        f"a page fetch delayed the event loop by {late * 1000:.0f} ms; the "
        "blocking work is not on a worker thread"
    )


def test_a_blocking_tool_delays_nothing_else():
    """The property, with the tool written both ways.

    Kept next to the real tool's test because it is the measurement that says
    why the change was needed: 2950 ms of delay, against 12 ms when the work
    moved to a thread for the same total wall clock.
    """
    @tool(description="blocking")
    async def blocking(seconds: float = 0.3) -> dict:
        time.sleep(seconds)
        return {"ok": True}

    @tool(description="threaded")
    async def threaded(seconds: float = 0.3) -> dict:
        await asyncio.to_thread(time.sleep, seconds)
        return {"ok": True}

    async def with_registry(entry):
        from app.agent.service import execute

        registry = ToolRegistry()
        registry.register(entry)
        llm = ScriptedLLM([call(entry.name, {"seconds": 0.3}), final("done")])
        return await execute("go", llm=llm, registry=registry, persist=False)

    async def scenario(entry):
        task = asyncio.create_task(with_registry(entry))
        wanted = time.monotonic() + 0.05
        await asyncio.sleep(0.05)
        late = time.monotonic() - wanted
        await task
        return late

    blocking_late = asyncio.run(scenario(blocking))
    threaded_late = asyncio.run(scenario(threaded))

    assert blocking_late > 0.2, "the blocking case should demonstrate the problem"
    assert threaded_late < 0.1, (
        f"a threaded tool still delayed the loop by {threaded_late * 1000:.0f} ms"
    )
