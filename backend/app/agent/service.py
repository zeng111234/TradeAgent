"""Agent service — the seam between the runtime and the application.

Everything above this module (CLI, HTTP, CI) should not know how an
``AgentLoop`` is wired. Everything below it (the loop, the registry, the
renderer) should not know about settings or the database.

Two things live here:

* :func:`build_llm_client` — pick a real model if one is configured.
* :func:`execute` — resolve tools, run the loop, persist the trace, and
  return a result that is ready to display.

If no API key is configured, :func:`execute` raises :class:`AgentUnavailable`
rather than silently falling back to a canned answer. A silent fallback would
make an unconfigured deployment look like a working one, which is exactly the
class of bug this refactor exists to remove.
"""
from __future__ import annotations

import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.config import settings

from .loop import AgentBudget, AgentRun, run_agent
from .protocol import LLMClient, OpenAICompatibleClient
from .render import render_html, render_mermaid_flowchart, render_mermaid_sequence
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

# Default ceilings for a user-triggered run. Deliberately modest: a runaway
# agent that burns a month of tokens overnight is worse than one that stops
# early and says so.
DEFAULT_BUDGET = AgentBudget(
    max_rounds=12,
    max_tool_calls=25,
    max_tokens=60_000,
    max_seconds=180.0,
)

# Ceilings for the unattended scheduled run, which must finish inside the CI
# job's 15-minute timeout with room to spare.
SCHEDULED_BUDGET = AgentBudget(
    max_rounds=10,
    max_tool_calls=22,
    max_tokens=45_000,
    max_seconds=300.0,
)


class AgentUnavailable(RuntimeError):
    """Raised when no model is configured, instead of faking a result."""


@dataclass
class AgentResult:
    """A finished run plus everything needed to display it."""

    run: AgentRun
    mermaid_sequence: str
    mermaid_flowchart: str
    html: str
    # Sub-agent runs spawned by this one, if delegation was enabled.
    delegations: Optional[dict] = None

    @property
    def status(self) -> str:
        return self.run.status

    def as_dict(self) -> dict:
        payload = self.run.as_dict()
        payload["mermaid_sequence"] = self.mermaid_sequence
        payload["mermaid_flowchart"] = self.mermaid_flowchart
        if self.delegations is not None:
            payload["delegations"] = self.delegations
        return payload


def is_configured() -> bool:
    """True when a real model can be reached."""
    return bool(settings.OPENAI_API_KEY and settings.OPENAI_BASE_URL and settings.OPENAI_MODEL)


def build_llm_client(timeout: float = 120.0) -> LLMClient:
    if not settings.OPENAI_API_KEY:
        raise AgentUnavailable(
            "No LLM is configured. Set OPENAI_API_KEY (and optionally "
            "OPENAI_BASE_URL / OPENAI_MODEL) in the environment or backend/.env."
        )
    return OpenAICompatibleClient(
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_BASE_URL,
        model=settings.OPENAI_MODEL,
        timeout=timeout,
    )


def spill_root() -> Path:
    """Where trimmed observations are kept so compaction stays lossless.

    Every run gets its own directory under this root. Shared would have been
    wrong: ``recall_observation`` resolves a name against a store, so a shared
    root would let one run read another's trimmed observations, and names are
    content hashes rather than secrets. Configurable via ``AGENT_SPILL_DIR``.
    """
    configured = (os.environ.get("AGENT_SPILL_DIR") or "").strip()
    base = Path(configured) if configured else Path(tempfile.gettempdir()) / "tradeagent-spill"
    return base / uuid.uuid4().hex


def build_registry() -> ToolRegistry:
    """The tool whitelist for a normal run."""
    from .trade_tools import build_trade_registry

    return build_trade_registry()


async def execute(
    goal: str,
    *,
    context: str = "",
    budget: Optional[AgentBudget] = None,
    llm: Optional[LLMClient] = None,
    registry: Optional[ToolRegistry] = None,
    persist: bool = True,
    spill_dir: Optional[Path] = None,
    allow_delegation: bool = True,
    run_id: Optional[str] = None,
    on_step: Optional[Callable[[Any], None]] = None,
) -> AgentResult:
    """Run the agent and return a displayable result.

    Parameters allow tests and the CLI to inject a fake model or a stubbed
    registry without touching settings or the network.

    The caller's ``registry`` is never mutated. Each run works on a scope built
    from it, because ``spawn_researcher`` binds a per-run delegation record and
    the loop adds ``recall_observation``; both used to be written into whatever
    registry was passed in, which broke the moment one registry served two runs.

    ``run_id`` lets the caller know the id before the run starts, so the row can
    be created up front and a poller can find it while the work is happening.
    ``on_step`` is called after every round, which is how a long analysis reports
    progress instead of going quiet.
    """
    if not (goal or "").strip():
        raise ValueError("goal must not be empty")

    client = llm or build_llm_client()
    spill = Path(spill_dir) if spill_dir is not None else spill_root()

    # Delegation is opt-out rather than opt-in: the whole point is that a run
    # which would otherwise drag every page it reads into one context can push
    # the reading somewhere else. Depth 1, so a child cannot delegate again.
    record = None
    if allow_delegation:
        from .subagent import attach_delegation

        tools, record = attach_delegation(
            registry or build_registry(), llm=client, spill_dir=spill
        )
    else:
        tools = registry or build_registry()

    run = await run_agent(
        goal.strip(),
        client,
        tools,
        budget=budget or DEFAULT_BUDGET,
        context=context,
        spill_dir=spill,
        run_id=run_id,
        on_step=on_step,
    )

    if persist:
        await _persist(run)

    result = AgentResult(
        run=run,
        mermaid_sequence=render_mermaid_sequence(run),
        mermaid_flowchart=render_mermaid_flowchart(run),
        html=render_html(run),
    )
    if record is not None:
        result.delegations = record.as_dict()
    return result


async def _persist(run: AgentRun) -> None:
    """Best-effort persistence. A trace that cannot be stored must not lose the run.

    Creates the schema first: CLI and cron entry points do not run the FastAPI
    lifespan, so the table may not exist yet.
    """
    try:
        from app.database import async_session, init_db

        from .repository import save_run

        await init_db()
        async with async_session() as db:
            await save_run(db, run)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not persist agent run %s: %s", run.run_id, exc)


def summarise(result: AgentResult) -> dict[str, Any]:
    """Compact shape for logs, CI output and API responses.

    Includes the delegated totals. ``tool_calls`` and ``*_tokens`` are what the
    parent loop spent; the ``*_total`` fields are what **the task** spent. The
    two differ whenever a sub-agent ran, and the difference is easy to miss.
    """
    run = result.run
    delegations = result.delegations or {}
    delegated_calls = delegations.get("tool_calls", 0)
    delegated_tokens = delegations.get("tokens", 0)
    return {
        "run_id": run.run_id,
        "status": run.status,
        "stop_reason": run.stop_reason,
        "tool_sequence": run.tool_sequence,
        "rounds": len(run.steps),
        "tool_calls": run.tool_call_count,
        "did_investigate": run.did_investigate,
        "elapsed": round(run.elapsed, 2),
        "prompt_tokens": run.prompt_tokens,
        "completion_tokens": run.completion_tokens,
        "final_output": run.final_output,
        "error": run.error,
        "delegations": delegations.get("spawned", 0),
        "delegations_refused": delegations.get("refused", 0),
        "delegated_tool_calls": delegated_calls,
        "tool_calls_total": run.tool_call_count + delegated_calls,
        "tokens_total": run.prompt_tokens + run.completion_tokens + delegated_tokens,
    }
