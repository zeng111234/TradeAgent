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
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import AgentBudget
from app.agent.render import derive_stats, render_html, render_mermaid_sequence
from app.agent.repository import get_run, list_runs, trace_of
from app.agent.service import (
    DEFAULT_BUDGET,
    AgentUnavailable,
    execute,
    is_configured,
    summarise,
)
from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["AI Agent"])

#: How many runs may be in flight at once. Past this a new one is refused with
#: 429 rather than queued: an unbounded queue is a backlog nobody can see, and
#: each run holds a model connection and writes to the same SQLite file.
MAX_CONCURRENT_RUNS = 3

_in_flight: set[str] = set()


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
        raise HTTPException(
            status_code=503,
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
            raise HTTPException(
                status_code=404, detail=f"no customer with id {data.customer_id}"
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

    try:
        result = await execute(
            data.goal, context=data.context, budget=budget,
            customer_id=data.customer_id,
        )
    except AgentUnavailable as exc:            # pragma: no cover - guarded above
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload = summarise(result)
    payload["steps"] = result.run.as_dict()["steps"]
    payload["mermaid_sequence"] = result.mermaid_sequence
    payload["mermaid_flowchart"] = result.mermaid_flowchart
    return payload


async def _start_in_background(data: AgentRunRequest, budget: AgentBudget) -> AgentRunStarted:
    """Write the row, hand out the id, and let the work happen elsewhere."""
    if len(_in_flight) >= MAX_CONCURRENT_RUNS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"{len(_in_flight)} analyses are already running and the limit is "
                f"{MAX_CONCURRENT_RUNS}. Poll /agent/runs for the ones in flight "
                "rather than starting another; each one holds a model connection "
                "and writes to the same database."
            ),
        )

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


@router.get("/runs/{run_id}")
async def api_agent_run_detail(run_id: str, db: AsyncSession = Depends(get_db)):
    """One run with its full trace and diagrams."""
    row = await get_run(db, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")

    trace = trace_of(row)
    diagram_input = {
        "goal": row.goal,
        "status": row.status,
        "stop_reason": row.stop_reason,
        "steps": trace,
    }
    return {
        "run_id": row.run_id,
        "goal": row.goal,
        "status": row.status,
        "stop_reason": row.stop_reason,
        "error": row.error,
        "final_output": row.final_output,
        "rounds": row.rounds,
        "tool_calls": row.tool_calls,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "elapsed_ms": row.elapsed,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "steps": trace,
        "mermaid_sequence": render_mermaid_sequence(diagram_input),
    }


@router.get("/runs/{run_id}/trace.html")
async def api_agent_run_html(run_id: str, db: AsyncSession = Depends(get_db)):
    """Standalone HTML page for one run.

    Plain HTML with a Mermaid CDN script, so it can be saved and opened without
    the frontend, and so it can be attached to a bug report.
    """
    row = await get_run(db, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no run with id {run_id}")

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
