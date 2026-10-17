"""A hundred requests at once: what actually gives way.

Measured on the real ASGI app (probes kept out of the repo; the numbers are in
docs/问答记录.md R17, repeated here because the assertions depend on them).
Hundred-request bursts, 500-customer book, before the fixes below:

    list customers, 100 concurrent   0.94 s wall, p50  500 ms, p95  891 ms, 0 errors
    follow-up plan, 100 concurrent   5.97 s wall, p50 3352 ms, p95 5828 ms, 0 errors
    customer debug, 100 concurrent   0.97 s wall, p50  539 ms, p95  907 ms, 0 errors
    /health during all three         p50 0 ms, max 16 ms

So the event loop holds -- that is measured, not assumed. Nothing in this file is
about the loop; it is about the two things the loop is not:

* **the connection pool** -- five sessions plus ten overflow, and a hundred
  requests queue behind them. Not an error at a hundred, just slower;
* **anything that holds a pooled connection while doing non-database work** --
  which is where the failures come from, because fifteen of those is the pool.

And a number that argues against the framing: concurrency is *slower* here than
doing the work one at a time. Same endpoint, measured both ways:

    1 request alone   47 ms
    4 sequential      0.08 s      4 concurrent      0.14 s   (1.81x slower)
    20 sequential     0.52 s     20 concurrent      0.92 s   (1.78x slower)

Queueing behind fifteen connections does not pay for itself in a single-process
SQLite service. After the pool timeout was shortened, 100 concurrent heavy
requests settled at 95 x 200 / 5 x 503 -- the ones that would have waited more
than five seconds now get an honest "busy, retry" instead of a thirty-second hang.
"""
from __future__ import annotations

import asyncio

import pytest
import sqlalchemy

from app.database import (
    DEFAULT_MAX_OVERFLOW,
    DEFAULT_POOL_SIZE,
    DEFAULT_POOL_TIMEOUT_SECONDS,
    engine,
)


# --------------------------------------------------------------------------
# The pool is a stated decision, not an inherited default
# --------------------------------------------------------------------------

def test_the_pool_shape_is_explicit_and_pinned():
    """These numbers are load-bearing and were previously SQLAlchemy's defaults.

    Pinned because a change here changes the failure mode: too small and
    requests queue, too large on SQLite and they contend instead (that is
    measured, not assumed -- see R15).
    """
    pool = engine.pool
    assert pool.size() == DEFAULT_POOL_SIZE
    assert pool._max_overflow == DEFAULT_MAX_OVERFLOW
    assert pool.timeout() == DEFAULT_POOL_TIMEOUT_SECONDS
    assert DEFAULT_POOL_SIZE + DEFAULT_MAX_OVERFLOW >= 10, (
        "a pool smaller than this cannot serve a browser opening several tabs"
    )


def test_the_pool_timeout_is_short_enough_to_fail_honestly():
    """Thirty seconds of hanging, then a 500, is the worst of both.

    The caller learns nothing for half a minute and then is told the server is
    broken when it is merely busy.
    """
    assert DEFAULT_POOL_TIMEOUT_SECONDS <= 10


# --------------------------------------------------------------------------
# Exhaustion answers 503 with a retry hint, not 500
# --------------------------------------------------------------------------

def test_pool_exhaustion_is_a_service_unavailable_not_a_server_error(client):
    """The handler exists, is registered, and returns the retryable shape."""
    from app.main import app

    handler = app.exception_handlers.get(sqlalchemy.exc.TimeoutError)
    assert handler is not None, (
        "a pool timeout would surface as a 500: an operator would go looking for "
        "a bug, and a client retrying a 500 is retrying a request the server "
        "never started"
    )

    class FakeRequest:
        method = "GET"

        class url:
            path = "/api/v1/customers/"

    response = asyncio.run(handler(FakeRequest(), sqlalchemy.exc.TimeoutError("pool")))

    assert response.status_code == 503
    assert response.headers.get("Retry-After")
    body = response.body.decode()
    assert "capacity" in body
    assert "not started" in body, (
        "the caller has to know the request did not happen, so retrying is safe"
    )


# --------------------------------------------------------------------------
# A long run must not hold a connection
# --------------------------------------------------------------------------

def test_an_agent_run_holds_no_pooled_connection_while_it_works(client):
    """The worst offender before this round, and the reason the run route lost its
    ``Depends(get_db)``.

    ``wait=true`` awaited ``execute(...)`` while holding the request-scoped
    session, so each in-flight run pinned one of the fifteen connections for up
    to ``max_seconds`` -- nine hundred in the worst case. Fifteen concurrent runs
    and the pool is empty: every other endpoint in the process waits, then fails.

    Two assertions, and the first is the one that cannot pass by accident: a
    session is checked out lazily, so "no connection was in use" would be true
    even for the old code on a path that never queried. The route having no
    database dependency at all is the property, not a proxy for it.
    """
    import inspect

    import app.routers.agent_runs as routes
    from app.agent.loop import AgentRun as RunResult
    from app.agent.service import AgentResult

    parameters = inspect.signature(routes.api_agent_run).parameters
    assert "db" not in parameters, (
        "the route takes a request-scoped session again; with wait=true that "
        "session is held for the whole run"
    )

    observed = {}

    async def slow_execute(goal, *, customer_id=None, **kwargs):
        # Simulates the middle of a long run: the loop is awaiting a model call,
        # not touching the database.
        observed["checked_out_during_run"] = engine.pool.checkedout()
        await asyncio.sleep(0.05)
        observed["checked_out_still"] = engine.pool.checkedout()
        run = RunResult(goal=goal, customer_id=customer_id, status="succeeded",
                        stop_reason="final answer")
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    original_configured, original_execute = routes.is_configured, routes.execute
    routes.is_configured = lambda: True
    routes.execute = slow_execute
    try:
        response = client.post(
            "/api/v1/agent/run",
            json={"goal": "review the pipeline", "wait": True},
        )
    finally:
        routes.is_configured, routes.execute = original_configured, original_execute

    assert response.status_code == 200, response.text
    assert observed["checked_out_during_run"] == 0, (
        "a run is holding a pooled connection while it works; fifteen of those "
        "would starve every other endpoint"
    )
    assert observed["checked_out_still"] == 0


def test_a_customer_check_uses_its_own_short_session(client):
    """The one query the route still needs must not keep a connection alive.

    Same property, tested through the branch that actually touches the database.
    """
    import app.routers.agent_runs as routes
    from app.agent.loop import AgentRun as RunResult
    from app.agent.service import AgentResult

    observed = {}

    async def slow_execute(goal, *, customer_id=None, **kwargs):
        observed["checked_out"] = engine.pool.checkedout()
        run = RunResult(goal=goal, customer_id=customer_id, status="succeeded",
                        stop_reason="final answer")
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    original_configured, original_execute = routes.is_configured, routes.execute
    routes.is_configured = lambda: True
    routes.execute = slow_execute
    try:
        response = client.post(
            "/api/v1/agent/run",
            json={"goal": "review this account", "customer_id": 4, "wait": True},
        )
    finally:
        routes.is_configured, routes.execute = original_configured, original_execute

    assert response.status_code == 200, response.text
    assert observed["checked_out"] == 0, (
        "the customer lookup's session was still checked out when the run started"
    )


# --------------------------------------------------------------------------
# What a hundred requests must NOT do
# --------------------------------------------------------------------------

def test_a_hundred_requests_are_all_served(client):
    """Small version of the measured burst, so a regression that starts dropping
    requests under concurrency shows up as a failing test rather than a slow
    afternoon.

    Uses a thread pool because ``TestClient`` is synchronous; the point is that
    the app answers all hundred, not the exact latency.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(
            lambda _: client.get("/api/v1/customers/?page_size=5"),
            range(100),
        ))

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 100, (
        f"under concurrency the app returned: {sorted(set(statuses))}"
    )
