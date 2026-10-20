"""Agent run API.

Adds goal-driven runs on top of the existing ``/agent/*`` endpoints. The
existing routes are untouched: they are the reviewed, human-triggered tools,
and this is the autonomous path.

A run can take minutes, so ``POST /agent/run`` supports two shapes:

* ``wait=false`` (the default) starts the run in the background and answers
  ``202`` with the run id and a poll URL. The row is written *before* any work
  happens, so polling works immediately rather than 404-ing until it finishes.
* ``wait=true`` keeps the connection open and returns the finished trace. That
  is what the CLI and the tests use, and it is what an operator clicking a
  button wants when the run is short.

Neither blocks the process: the work is asynchronous, and the tools that do
blocking I/O (page fetches, web search, parsing) run on worker threads. That
second part is not optional -- measured, three blocking page fetches delayed
every other coroutine in the process by 2950 ms, and moving them to a thread
brought that to 12 ms with no increase in wall-clock time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError, ErrorCode
from app.agent.loop import AgentBudget
from app.agent.render import derive_stats, render_html, render_mermaid_sequence
from app.agent.repository import TERMINAL_STATUSES, get_run, list_runs, trace_of
from app.agent.service import (
    DEFAULT_BUDGET,
    AgentUnavailable,
    execute,
    is_configured,
    summarise,
)
from app.database import async_session, get_db
from app.timeouts import BROWSER_REQUEST_SECONDS, WAIT_MODE_MAX_SECONDS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["AI Agent"])

#: How many runs may be in flight at once. Past this a new one is refused with
#: 429 rather than queued: an unbounded queue is a backlog nobody can see, and
#: each run holds a model connection and writes to the same SQLite file.
#:
#: Both entry shapes count against it -- the background one and ``wait=true``.
#: The reason the limit exists (a model connection each, writes to one database)
#: is as true of a synchronous run, and a shedding boundary with a hole in it is
#: not a boundary: ``wait=true`` used to bypass the count entirely, so three
#: background runs plus any number of synchronous ones all ran at once.
MAX_CONCURRENT_RUNS = 3

_in_flight: set[str] = set()


def _refuse_when_full() -> ApiError:
    """The one message for "at capacity", for both run shapes.

    ``Retry-After`` because this is the one 429 where the server knows what
    "later" means: a run is bounded, and a slot frees when one finishes. A 429
    without it leaves the client guessing between one second and one minute --
    and guessing short is how a refusal becomes a retry storm.
    """
    return ApiError(
        status_code=429,
        code=ErrorCode.AGENT_AT_CAPACITY,
        detail=(
            f"{len(_in_flight)} analyses are already running and the limit is "
            f"{MAX_CONCURRENT_RUNS}. Each one holds a model connection and "
            "writes to the same database, so this request was refused rather "
            "than queued -- an unbounded queue is a backlog nobody can see. "
            "Poll /api/v1/agent/runs for the ones in flight and retry when one "
            "finishes."
        ),
        retryable=True,
        details={"in_flight": len(_in_flight), "limit": MAX_CONCURRENT_RUNS},
        headers={"Retry-After": "5"},
    )


class AgentRunRequest(BaseModel):
    goal: str = Field(..., min_length=1, description="What the agent should achieve")
    # Named when the run is about one customer. Recorded on the run row, where
    # it can be indexed -- without it, finding a customer's runs means reading
    # every trace and looking for their id in the text.
    customer_id: Optional[int] = Field(None, description="The customer this run is about")
    context: str = Field("", description="Optional background for this run")
    max_rounds: Optional[int] = Field(None, ge=1, le=30)
    max_tool_calls: Optional[int] = Field(None, ge=1, le=60)
    max_seconds: Optional[float] = Field(None, gt=0, le=900)
    wait: bool = Field(
        False,
        description=(
            "false: return 202 with the run id immediately and let it finish in "
            "the background. true: hold the connection and return the full trace."
        ),
    )


class AgentRunStarted(BaseModel):
    """What a caller gets when the run is still going."""
    run_id: str
    status: str
    goal: str
    poll: str
    trace_url: str
    max_seconds: float


class AgentRunSummary(BaseModel):
    run_id: str
    status: str
    stop_reason: str
    tool_sequence: list[str]
    rounds: int
    tool_calls: int
    did_investigate: bool
    elapsed: float
    prompt_tokens: int
    completion_tokens: int
    #: Of ``prompt_tokens``, how many the provider served from its cache. Defaulted
    #: so runs recorded before the column existed still validate.
    cached_prompt_tokens: int = 0
    final_output: str
    error: Optional[str] = None
    steps: list[dict[str, Any]] = []
    mermaid_sequence: str = ""
    mermaid_flowchart: str = ""
    # Delegated work is work this task did, and the parent's counters cannot see
    # it. Without these a task that spawned five sub-agents looks like it made
    # one tool call.
    delegations: int = 0
    delegations_refused: int = 0
    delegated_tool_calls: int = 0
    tool_calls_total: int = 0
    tokens_total: int = 0


@router.get("/capabilities")
async def api_agent_capabilities():
    """What the agent can and cannot do.

    Published explicitly rather than left to be inferred, because the boundary
    is the point: an operator should be able to confirm without reading the
    source that the agent cannot send mail.
    """
    from app.agent.service import build_registry

    registry = build_registry()
    return {
        "configured": is_configured(),
        "tool_count": len(registry),
        "tools": [
            {
                "name": entry["function"]["name"],
                "description": entry["function"]["description"],
                "parameters": sorted(entry["function"]["parameters"].get("properties", {})),
                "required": sorted(entry["function"]["parameters"].get("required", [])),
            }
            for entry in registry.schemas()
        ],
        "write_capable": sorted(t.name for t in registry.tagged("write")),
        "cannot": [
            "send email to a customer",
            "delete or modify an existing customer",
            "delete or modify an email log",
            # Absent capabilities, not policies. "We refuse to quote" would be
            # a promise the code does not keep -- the model can still write a
            # price into a draft. What is true is that there is nothing to read
            # a price or a schedule from, so any number in a draft came from
            # whoever wrote the brief, not from this system.
            "quote a price from a price list or a live freight rate: there is none",
            "promise a ship date: there is no schedule or carrier integration",
            "export contacts in bulk: the list view omits contact details, and "
            "each customer is a separate lookup",
        ],
        "sending": "Drafts are created with status 'pending'. A human sends them "
                   "through the existing review flow.",
    }


@router.post("/run", response_model=None)
async def api_agent_run(
    data: AgentRunRequest,
    response: Response,
):
    """Run the agent against a goal.

    Returns the full trace when ``wait`` is true, and a run id with a poll URL
    (HTTP 202) when it is false. The run row exists either way before any work
    starts.

    **No request-scoped session.** This route used to take ``db=Depends(get_db)``
    and hold it across ``execute(...)``, which with ``wait=true`` means holding one
    of the fifteen pooled connections for the length of a run -- up to
    ``max_seconds``, and nine hundred seconds in the worst case. Measured: the
    pool is fifteen and the event loop is not the constraint, so a handful of
    long runs would starve every other endpoint in the process. The one query
    this route needs (does the customer exist) now gets its own short-lived
    session, and nothing is pinned while the agent works.
    """
    if not is_configured():
        raise ApiError(
            status_code=503,
            code=ErrorCode.LLM_NOT_CONFIGURED,
            detail=(
                "No LLM configured. Set OPENAI_API_KEY (and optionally "
                "OPENAI_BASE_URL / OPENAI_MODEL). There is no fallback: an agent "
                "that cannot decide is not an agent."
            ),
        )

    if data.customer_id is not None:
        # Checked rather than stored blind. SQLite does not enforce the foreign
        # key unless it is switched on, so a wrong id would be written and then
        # quietly match nothing -- a run recorded as being about a customer who
        # does not exist is worse than a rejected request.
        from sqlalchemy import select

        from app.database import async_session
        from app.models.customer import Customer

        async with async_session() as session:
            exists = (
                await session.execute(
                    select(Customer.id).where(Customer.id == data.customer_id)
                )
            ).scalar_one_or_none()
        if exists is None:
            raise ApiError(
                status_code=404, code=ErrorCode.CUSTOMER_NOT_FOUND, detail=f"no customer with id {data.customer_id}"
            )

    budget = DEFAULT_BUDGET
    if any((data.max_rounds, data.max_tool_calls, data.max_seconds)):
        budget = AgentBudget(
            max_rounds=data.max_rounds or DEFAULT_BUDGET.max_rounds,
            max_tool_calls=data.max_tool_calls or DEFAULT_BUDGET.max_tool_calls,
            max_seconds=data.max_seconds or DEFAULT_BUDGET.max_seconds,
            max_tokens=DEFAULT_BUDGET.max_tokens,
        )

    if not data.wait:
        return await _start_in_background(data, budget)

    # `wait=true` holds this HTTP connection open, so its ceiling is a property of
    # the transport rather than of the work: the browser gives up at
    # BROWSER_REQUEST_SECONDS, and a run that outlives it produces a timeout error
    # for work that is going fine. Two cases, and they are different:
    #
    # * the caller asked for a budget the connection cannot carry -> refuse, and
    #   name the path that can (``wait=false``, whose row exists up front);
    # * the caller asked for nothing specific -> clamp to what the connection can
    #   carry. The default run budget is 180s, so this is the common case.
    if data.max_seconds and data.max_seconds > WAIT_MODE_MAX_SECONDS:
        raise ApiError(
            status_code=422,
            code=ErrorCode.WAIT_BUDGET_TOO_LONG,
            detail=(
                f"wait=true holds this HTTP request open, so the budget must stay "
                f"under {WAIT_MODE_MAX_SECONDS:.0f}s (the browser waits "
                f"{BROWSER_REQUEST_SECONDS:.0f}s). Asked for "
                f"{data.max_seconds:.0f}s. Use wait=false and poll "
                "/api/v1/agent/runs/<run_id>: the run row exists before the work "
                "starts, so polling works from the first moment."
            ),
        )
    if budget.max_seconds > WAIT_MODE_MAX_SECONDS:
        budget = AgentBudget(
            max_rounds=budget.max_rounds,
            max_tool_calls=budget.max_tool_calls,
            max_seconds=WAIT_MODE_MAX_SECONDS,
            max_tokens=budget.max_tokens,
        )

    # Same gate as the background path. A synchronous run holds a model
    # connection and writes to the same database for its whole duration, so it
    # counts; the only difference is who waits for the answer.
    gate_key = f"wait:{uuid.uuid4().hex}"
    if len(_in_flight) >= MAX_CONCURRENT_RUNS:
        raise _refuse_when_full()
    _in_flight.add(gate_key)
    try:
        result = await execute(
            data.goal, context=data.context, budget=budget,
            customer_id=data.customer_id,
        )
    except AgentUnavailable as exc:            # pragma: no cover - guarded above
        raise ApiError(status_code=503, code=ErrorCode.LLM_NOT_CONFIGURED, detail=str(exc)) from exc
    except ValueError as exc:
        raise ApiError(status_code=422, code=ErrorCode.VALIDATION_ERROR, detail=str(exc)) from exc
    finally:
        # Released on every exit -- a refused or failed run must not leak a
        # slot, or the limit slowly closes over the process.
        _in_flight.discard(gate_key)

    payload = summarise(result)
    payload["steps"] = result.run.as_dict()["steps"]
    payload["mermaid_sequence"] = result.mermaid_sequence
    payload["mermaid_flowchart"] = result.mermaid_flowchart
    return payload


async def _start_in_background(data: AgentRunRequest, budget: AgentBudget) -> AgentRunStarted:
    """Write the row, hand out the id, and let the work happen elsewhere."""
    if len(_in_flight) >= MAX_CONCURRENT_RUNS:
        raise _refuse_when_full()

    from app.agent.service import _persist
    from app.database import async_session
    from app.agent.repository import save_run, start_run

    run_id = uuid.uuid4().hex

    # The row goes in first. Without it a poller gets 404 for the whole run,
    # which is exactly how "queue it and poll" failed to be true before.
    async with async_session() as db:
        from app.agent.loop import AgentRun as AgentRunResult

        await start_run(db, AgentRunResult(
            goal=data.goal, run_id=run_id, customer_id=data.customer_id,
        ))

    _in_flight.add(run_id)

    def _on_step(step, run) -> None:
        """Write progress while the run is still going.

        Called synchronously from the loop, so the write is scheduled rather
        than awaited: the loop must not wait on the database between rounds. A
        three-minute analysis that writes nothing is indistinguishable from one
        that died, which is the complaint this whole endpoint exists to fix.
        """
        task = asyncio.create_task(_persist_progress(run_id, run))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def _runner() -> None:
        try:
            result = await execute(
                data.goal, context=data.context, budget=budget,
                run_id=run_id, persist=False, on_step=_on_step,
                customer_id=data.customer_id,
            )
            async with async_session() as db:
                await save_run(db, result.run)
        except Exception as exc:  # noqa: BLE001 - a background failure must be visible
            logger.exception("background agent run %s failed", run_id)
            try:
                async with async_session() as db:
                    row = await get_run(db, run_id)
                    if row is not None:
                        row.status = "error"
                        row.error = f"{type(exc).__name__}: {exc}"
                        row.stop_reason = "background task failed"
                        await db.commit()
            except Exception:  # noqa: BLE001
                logger.exception("could not record the failure of run %s", run_id)
        finally:
            _in_flight.discard(run_id)

    # Held in a module-level set as well as the task, because a task that is not
    # referenced can be garbage collected mid-flight.
    task = asyncio.create_task(_runner())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return AgentRunStarted(
        run_id=run_id,
        status="running",
        goal=data.goal,
        poll=f"/api/v1/agent/runs/{run_id}",
        trace_url=f"/api/v1/agent/runs/{run_id}/trace.html",
        max_seconds=budget.max_seconds,
    )


#: Strong references to in-flight tasks. asyncio only keeps weak ones.
_background_tasks: set[asyncio.Task] = set()


async def drain_background_runs(timeout: float = 5.0) -> int:
    """Wait for in-flight background runs, briefly.

    Called on shutdown, so a run is not killed in the middle of writing its
    result. Whatever does not finish is left as ``running`` and swept up by
    ``mark_stale_runs`` on the next start -- which is why that sweep exists.

    Returns how many tasks were still running when the wait ended.
    """
    pending = {t for t in _background_tasks if not t.done()}
    if pending:
        done, still_running = await asyncio.wait(pending, timeout=timeout)
        if still_running:
            logger.warning(
                "%d agent run(s) still going after %.0fs; they will be marked "
                "abandoned on the next start", len(still_running), timeout,
            )
        return len(still_running)
    return 0


async def _persist_progress(run_id: str, run) -> None:
    """Update a running row with what it has done so far. Best effort."""
    try:
        from app.database import async_session
        from app.agent.repository import save_run

        async with async_session() as db:
            await save_run(db, run)
    except Exception:  # noqa: BLE001 - progress must never break the run
        logger.debug("could not persist progress for run %s", run_id, exc_info=True)


@router.get("/runs")
async def api_agent_runs(limit: int = 20, status: Optional[str] = None,
                         db: AsyncSession = Depends(get_db)):
    """Recent runs, newest first. The audit trail."""
    rows = await list_runs(db, limit=limit, status=status)
    listing = []
    for row in rows:
        steps = trace_of(row)
        stats = derive_stats(steps)
        listing.append(
            {
                "run_id": row.run_id,
                "goal": row.goal,
                "prompt_version": getattr(row, "prompt_version", None),
                "status": row.status,
                "stop_reason": row.stop_reason,
                # Derived from the trace, so the path shown for a run cannot
                # disagree with the trace you open for it.
                "tool_sequence": stats["tool_sequence"],
                "rounds": stats["rounds"],
                "tool_calls": stats["tool_calls"],
                "refused_calls": stats["refused_calls"],
                "elapsed_ms": row.elapsed,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    return {"count": len(listing), "runs": listing}


def _snapshot(row, *, with_diagram: bool = True) -> dict[str, Any]:
    """One run, in the shape both the detail endpoint and the stream send.

    Shared on purpose. The stream is a second publisher of the same facts, and
    two hand-built copies of this dictionary would drift -- the first one to
    drift would be a field the UI reads that only exists on one path.

    ``with_diagram=False`` for the stream: the Mermaid text is rebuilt from the
    whole trace on every call, and a stream may call this once a second for
    three minutes while the client is only rendering the timeline.
    """
    trace = trace_of(row)
    payload = {
        "run_id": row.run_id,
        "goal": row.goal,
        # Which instructions produced this run. Null for rows written before the
        # column existed -- null is the honest answer, not a guessed version.
        "prompt_version": getattr(row, "prompt_version", None),
        "status": row.status,
        "stop_reason": row.stop_reason,
        "error": row.error,
        "final_output": row.final_output,
        "rounds": row.rounds,
        "tool_calls": row.tool_calls,
        "prompt_tokens": row.prompt_tokens,
        "cached_prompt_tokens": getattr(row, "cached_prompt_tokens", 0) or 0,
        "completion_tokens": row.completion_tokens,
        "elapsed_ms": row.elapsed,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "steps": trace,
    }
    if with_diagram:
        payload["mermaid_sequence"] = render_mermaid_sequence({
            "goal": row.goal,
            "status": row.status,
            "stop_reason": row.stop_reason,
            "steps": trace,
        })
    return payload


@router.get("/runs/{run_id}")
async def api_agent_run_detail(run_id: str, db: AsyncSession = Depends(get_db)):
    """One run with its full trace and diagrams."""
    row = await get_run(db, run_id)
    if row is None:
        raise ApiError(status_code=404, code=ErrorCode.RUN_NOT_FOUND, detail=f"no run with id {run_id}")

    return _snapshot(row)


#: How often the stream looks for new progress. The loop writes once per round,
#: so a shorter interval would re-read the same row and send nothing.
STREAM_TICK_SECONDS = 1.0
#: A comment line every this often. A proxy closes a connection it believes is
#: idle after 30-60 s, and a run waiting on a slow model looks exactly like an
#: idle connection. The comment is ignored by every EventSource/fetch parser,
#: which is why it is a comment and not an event.
STREAM_HEARTBEAT_SECONDS = 15.0
#: How far past the default budget one connection may live. The client resumes
#: with ``after``, so this bounds a zombie connection, not a run.
STREAM_GRACE_SECONDS = 600.0


def _sse(event: str, data: dict, event_id: Optional[int] = None) -> str:
    """One Server-Sent Event.

    ``json.dumps`` never leaves a raw newline inside a string, so a single
    ``data:`` line carries the whole payload. SSE's multi-line form exists for
    text that contains newlines; JSON has already escaped them.
    """
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


@router.get("/runs/{run_id}/events")
async def api_agent_run_events(run_id: str, request: Request, after: int = 0):
    """Follow one run as it happens.

    Why a stream when ``GET /runs/{id}`` already returns the same snapshot:
    polling costs one request per second per open tab, and every one of them
    re-reads and re-serialises the whole trace. A stream holds one connection
    and pushes only when the round number moves.

    **Resumption is the client's job and the server's contract.** Every event
    carries ``id: <round>``. A client that loses the connection reconnects with
    ``?after=<last id>`` and gets no history it already has. Two consequences
    worth stating:

    * an event id is a *position*, not a sequence number: two events can carry
      the same id, because the terminal ``done`` follows the ``snapshot`` of
      the final round;
    * a position past the end is not an error. The first tick always sends the
      current state, so a client that reconnects after the run finished is
      answered immediately instead of waiting for a round that will never come.
    """
    async with async_session() as session:
        row = await get_run(session, run_id)
        if row is None:
            raise ApiError(
                status_code=404,
                code=ErrorCode.RUN_NOT_FOUND,
                detail=f"no run with id {run_id}",
            )
        budget_seconds = float(DEFAULT_BUDGET.max_seconds)

    last_sent = max(0, after)

    async def generate():
        nonlocal last_sent
        started = time.monotonic()
        deadline = started + budget_seconds + STREAM_GRACE_SECONDS
        last_beat = started
        first_tick = True

        while True:
            if await request.is_disconnected():
                # The client left. Not an error: this is the normal end of a
                # stream whose tab was closed.
                return

            # A short-lived session per tick, never one held for the stream. A
            # stream lives for minutes; the pool has fifteen connections and
            # every other endpoint shares them.
            async with async_session() as session:
                fresh = await get_run(session, run_id)

            if fresh is None:
                yield _sse(
                    "error",
                    {"code": "run_not_found", "run_id": run_id},
                    last_sent + 1,
                )
                return

            rounds = fresh.rounds or 0
            terminal = fresh.status in TERMINAL_STATUSES

            # Send when the round moved, and always send the terminal state: a
            # reconnecting client has `last_sent == rounds` and still needs the
            # final answer. The first tick always sends, so a client with a
            # stale or ahead-of-the-server cursor is resynchronised at once.
            if first_tick or rounds > last_sent or terminal:
                first_tick = False
                last_sent = rounds
                snapshot = _snapshot(fresh, with_diagram=False)
                yield _sse("done" if terminal else "snapshot", snapshot, last_sent)
                if terminal:
                    return

            now = time.monotonic()
            if now - last_beat >= STREAM_HEARTBEAT_SECONDS:
                last_beat = now
                yield ": heartbeat\n\n"
            if now > deadline:
                yield _sse(
                    "timeout",
                    {
                        "code": "stream_timeout",
                        "message": (
                            "this connection has been open longer than any run is "
                            "allowed to take; reconnect with ?after=<last id>"
                        ),
                        "last_id": last_sent,
                    },
                    last_sent,
                )
                return
            await asyncio.sleep(STREAM_TICK_SECONDS)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            # A proxy that buffers this delivers the whole "stream" in one piece
            # when the run ends, which is precisely not the point.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{run_id}/trace.html")
async def api_agent_run_html(run_id: str, db: AsyncSession = Depends(get_db)):
    """Standalone HTML page for one run.

    Plain HTML with a Mermaid CDN script, so it can be saved and opened without
    the frontend, and so it can be attached to a bug report.
    """
    row = await get_run(db, run_id)
    if row is None:
        raise ApiError(status_code=404, code=ErrorCode.RUN_NOT_FOUND, detail=f"no run with id {run_id}")

    trace = trace_of(row)
    stats = derive_stats(trace)
    # Pass the derived numbers through so the rendered header agrees with the
    # timeline below it and with the listing the user clicked from.
    page = render_html({
        "goal": row.goal,
        "status": row.status,
        "stop_reason": row.stop_reason,
        # Without this the conclusion section rendered empty, and it is the part
        # a human actually reads.
        "final_output": row.final_output or "",
        "steps": trace,
        **stats,
        "elapsed": row.elapsed / 1000.0 if row.elapsed else 0,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
    })
    return HTMLResponse(content=page)
