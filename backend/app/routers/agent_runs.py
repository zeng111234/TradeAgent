"""Agent run API.

Adds goal-driven runs on top of the existing ``/agent/*`` endpoints. The
existing routes are untouched: they are the reviewed, human-triggered tools,
and this is the autonomous path.

One deliberate limitation: ``POST /agent/run`` is synchronous. A run can take
up to its wall-clock budget, so a caller holds the connection for that long.
That is fine for an operator clicking a button or for the CLI, and it keeps the
trace in the response where it is useful. A production deployment would queue
the run and poll ``GET /agent/runs/{run_id}``; the storage for that already
exists, so the change is additive rather than a redesign.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import AgentBudget
from app.agent.render import render_html, render_mermaid_sequence
from app.agent.repository import get_run, list_runs, trace_of
from app.agent.service import (
    DEFAULT_BUDGET,
    AgentUnavailable,
    execute,
    is_configured,
    summarise,
)
from app.database import get_db

router = APIRouter(prefix="/agent", tags=["AI Agent"])


class AgentRunRequest(BaseModel):
    goal: str = Field(..., min_length=1, description="What the agent should achieve")
    context: str = Field("", description="Optional background for this run")
    max_rounds: Optional[int] = Field(None, ge=1, le=30)
    max_tool_calls: Optional[int] = Field(None, ge=1, le=60)
    max_seconds: Optional[float] = Field(None, gt=0, le=900)


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


@router.post("/run", response_model=AgentRunSummary)
async def api_agent_run(data: AgentRunRequest):
    """Run the agent against a goal and return the trace.

    The full trace is returned so the caller can render the path the agent took
    rather than only its conclusion.
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

    budget = DEFAULT_BUDGET
    if any((data.max_rounds, data.max_tool_calls, data.max_seconds)):
        budget = AgentBudget(
            max_rounds=data.max_rounds or DEFAULT_BUDGET.max_rounds,
            max_tool_calls=data.max_tool_calls or DEFAULT_BUDGET.max_tool_calls,
            max_seconds=data.max_seconds or DEFAULT_BUDGET.max_seconds,
            max_tokens=DEFAULT_BUDGET.max_tokens,
        )

    try:
        result = await execute(data.goal, context=data.context, budget=budget)
    except AgentUnavailable as exc:            # pragma: no cover - guarded above
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload = summarise(result)
    payload["steps"] = result.run.as_dict()["steps"]
    payload["mermaid_sequence"] = result.mermaid_sequence
    payload["mermaid_flowchart"] = result.mermaid_flowchart
    return payload


@router.get("/runs")
async def api_agent_runs(limit: int = 20, status: Optional[str] = None,
                         db: AsyncSession = Depends(get_db)):
    """Recent runs, newest first. The audit trail."""
    rows = await list_runs(db, limit=limit, status=status)
    return {
        "count": len(rows),
        "runs": [
            {
                "run_id": r.run_id,
                "goal": r.goal,
                "status": r.status,
                "stop_reason": r.stop_reason,
                "rounds": r.rounds,
                "tool_calls": r.tool_calls,
                "elapsed_ms": r.elapsed,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


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

    page = render_html({
        "goal": row.goal,
        "status": row.status,
        "stop_reason": row.stop_reason,
        "steps": trace_of(row),
    })
    return HTMLResponse(content=page)
