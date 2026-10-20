"""The timeout ladder: one parameter is not enough, and the order matters.

There are five layers, and each has a different job:

1. **per attempt** -- one HTTP request to a model, one SMTP message;
2. **per logical call** -- that attempt *plus its retries*;
3. **per unit of work** -- one agent run, one batch send: budgets, not timeouts,
   because the loop length is not known in advance;
4. **per request** -- what the browser is willing to wait for;
5. **per resource** -- a pooled connection, or SQLite's single writer.

The rule the tests below enforce: **every layer is strictly shorter than the one
that waits on it.** Three inversions existed before this file:

* a child agent's 90-second allowance against a 120-second model timeout, so one
  call could outlive the child it belonged to;
* ``wait=true`` accepting a 900-second run while the browser waits 120;
* one SMTP message bounded at 60 seconds while a batch of fifty was unbounded --
  fifty minutes inside a process-wide lock.

The numbers are policy. The order is not.
"""
from __future__ import annotations

import asyncio

import pytest

from app import llm, timeouts


# --------------------------------------------------------------------------
# The order
# --------------------------------------------------------------------------

def test_the_browser_waits_longer_than_anything_served_synchronously():
    assert timeouts.WAIT_MODE_MAX_SECONDS < timeouts.BROWSER_REQUEST_SECONDS, (
        "a synchronous run may take longer than the client will wait, so the "
        "client reports a timeout for work that is going fine"
    )


def test_a_model_call_fits_inside_a_run():
    """Per logical call (attempts plus retries) must fit a run's budget.

    This is the inversion that was live: a sub-agent is given 90 seconds, and its
    model client was allowed 120 per attempt -- three attempts inside a child that
    had less than one attempt's worth of time.
    """
    assert timeouts.LLM_TOTAL_SECONDS <= timeouts.RUN_MAX_SECONDS
    assert timeouts.LLM_TOTAL_SECONDS <= timeouts.SUBAGENT_ALLOWANCE_SECONDS, (
        "a child would outlive its own allowance on a single model call"
    )


def test_one_tool_call_fits_inside_a_run():
    assert timeouts.TOOL_CALL_SECONDS < timeouts.RUN_MAX_SECONDS


@pytest.mark.asyncio
async def test_the_logical_call_budget_is_enforced_not_only_declared(monkeypatch):
    """A 60s attempt times the SDK's two retries outlives the 80s policy.

    The number was in ``timeouts.py`` and in this file's own docstring while the
    call itself had no total: the client would happily spend 120+ seconds, the
    browser gives up at 120, and the service kept working after the user had
    already seen a timeout. This pins the cancellation, not the constant.
    """

    class Slow:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    await asyncio.sleep(5)

    monkeypatch.setattr(llm, "get_client", lambda: Slow())
    monkeypatch.setattr(llm, "LLM_TOTAL_SECONDS", 0.05)

    started = asyncio.get_running_loop().time()
    with pytest.raises(llm.LLMTransient, match="budget"):
        await llm.complete_text("hello")
    elapsed = asyncio.get_running_loop().time() - started

    # A policy number that the call ignores is the failure this test exists for.
    assert elapsed < 1.0


def test_a_subagent_fits_inside_its_parent():
    assert timeouts.SUBAGENT_ALLOWANCE_SECONDS < timeouts.RUN_MAX_SECONDS


def test_a_batch_fits_inside_the_browser():
    assert timeouts.BATCH_SEND_BUDGET_SECONDS < timeouts.BROWSER_REQUEST_SECONDS


def test_one_message_fits_inside_its_batch():
    """Per attempt shorter than per unit of work, or the budget never bites."""
    assert timeouts.SMTP_SEND_SECONDS < timeouts.BATCH_SEND_BUDGET_SECONDS


def test_a_page_fetch_fits_inside_a_tool_call():
    assert timeouts.PAGE_FETCH_SECONDS < timeouts.TOOL_CALL_SECONDS


def test_waiting_for_a_connection_is_shorter_than_waiting_for_the_writer():
    """Queueing for a connection is not work; waiting for the writer is.

    The pool gives up quickly and answers 503 (see app/main.py). Once a
    connection is in hand, waiting up to the SQLite busy timeout for the single
    writer is real progress.
    """
    assert timeouts.POOL_CHECKOUT_SECONDS < timeouts.SQLITE_BUSY_SECONDS


# --------------------------------------------------------------------------
# The code actually uses these numbers
# --------------------------------------------------------------------------

def test_the_llm_seam_uses_the_ladder():
    from app import llm

    assert llm.DEFAULT_MAX_RETRIES == timeouts.LLM_MAX_RETRIES
    assert llm.DEFAULT_TIMEOUT_SECONDS == timeouts.LLM_REQUEST_SECONDS


def test_the_agent_budget_fits_inside_the_run_ceiling():
    from app.agent.loop import AgentBudget
    from app.agent.service import DEFAULT_BUDGET

    assert DEFAULT_BUDGET.max_seconds == timeouts.RUN_MAX_SECONDS
    assert AgentBudget().tool_timeout_seconds == timeouts.TOOL_CALL_SECONDS


def test_the_pool_uses_the_ladder():
    from app.database import DEFAULT_POOL_TIMEOUT_SECONDS

    assert DEFAULT_POOL_TIMEOUT_SECONDS == timeouts.POOL_CHECKOUT_SECONDS


def test_smtp_is_given_a_timeout_explicitly():
    """aiosmtplib's own default is 60 seconds; stating ours is the point.

    Without it the value is the library's, which changes when the library does --
    and this call happens while the process-wide send lock is held.
    """
    import inspect

    from app.utils import email_sender

    source = inspect.getsource(email_sender.send_email)
    assert "timeout=SMTP_SEND_SECONDS" in source, (
        "the SMTP call is back to relying on the library's default"
    )


def test_the_batch_has_a_clock_as_well_as_a_count():
    """`limit` bounds how many; only a deadline bounds how long."""
    import inspect

    from app.routers import emails

    source = inspect.getsource(emails.api_send_pending)
    assert "BATCH_SEND_BUDGET_SECONDS" in source
    assert "stopped_on_time" in source, (
        "a batch cut short by the clock must say so, or the caller thinks it "
        "received everything"
    )


# --------------------------------------------------------------------------
# Behaviour, not just constants
# --------------------------------------------------------------------------

def test_wait_mode_refuses_a_budget_the_connection_cannot_carry(client, monkeypatch):
    import app.routers.agent_runs as routes

    monkeypatch.setattr(routes, "is_configured", lambda: True)

    response = client.post(
        "/api/v1/agent/run",
        json={"goal": "a long analysis", "wait": True, "max_seconds": 600},
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "wait=false" in detail, "it has to name the path that can do this"
    assert "600" in detail


def test_wait_mode_clamps_the_default_budget_instead_of_refusing(client, monkeypatch):
    """Asking for nothing in particular must still work.

    The default run budget is 180s and the connection carries 100, so a
    ``wait=true`` run with no stated budget is clamped rather than rejected --
    the ceiling is a property of the transport, not a hidden cut to the caller's
    request.
    """
    import app.routers.agent_runs as routes
    from app.agent.loop import AgentRun as RunResult
    from app.agent.service import AgentResult

    seen = {}

    async def fake_execute(goal, *, budget=None, **kwargs):
        seen["max_seconds"] = budget.max_seconds if budget else None
        run = RunResult(goal=goal, status="succeeded", stop_reason="final answer")
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", fake_execute)

    response = client.post("/api/v1/agent/run", json={"goal": "short", "wait": True})

    assert response.status_code == 200, response.text
    assert seen["max_seconds"] == timeouts.WAIT_MODE_MAX_SECONDS


def test_a_model_call_is_cancelled_when_the_run_budget_runs_out():
    """The inner layer must not outlive the outer one, measured in behaviour."""
    import time as clock

    from app.agent.loop import AgentBudget, AgentLoop
    from app.agent.loop import AgentError
    from app.agent.protocol import LLMReply
    from app.agent.tools import ToolRegistry

    class SlowModel:
        async def chat(self, *_args, **_kwargs):
            await asyncio.sleep(5)
            return LLMReply(content="too late")

    loop = AgentLoop(
        SlowModel(),
        ToolRegistry(),
        budget=AgentBudget(max_rounds=3, max_seconds=0.4, tool_timeout_seconds=0.2),
    )

    started = clock.monotonic()
    with pytest.raises(AgentError) as caught:
        asyncio.run(loop.run("do something"))
    elapsed = clock.monotonic() - started

    assert elapsed < 2.0, (
        f"the run took {elapsed:.1f}s despite a 0.4s budget; the model call is not "
        "bounded by what is left of the run"
    )
    assert "budget" in str(caught.value)
