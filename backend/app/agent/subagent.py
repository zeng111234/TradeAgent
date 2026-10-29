"""Sub-agents: delegating a self-contained investigation.

Why this exists
---------------
A prospect scan that inspects ten candidate companies accumulates ten full page
texts in one conversation. Long before the context window is a problem, the
parent is carrying detail it has already finished with — the verdict on
candidate 3 does not need candidate 3's page text in the parent's history
forever.

A sub-agent gets its own conversation, does the reading, and returns a compact
result. The parent's context sees the conclusion, not the working.

What the reference implementations do, and what is copied here
--------------------------------------------------------------
DeepSeek Harness ships sub-agents as a first-class subsystem: ``maxDepth``
defaults to ``1``; the tool stays visible at the cap and each attempt re-checks
the caller's depth; ``maxActiveSubagents`` defaults to 8 and admission does not
queue, "because a parent waiting for descendants must not wait for its own
occupied slot"; a one-shot child returns its final answer as the tool result.
Pi deliberately omits sub-agents entirely.

The shape here follows DSH on the parts that matter and stays simpler where
TradeAgent's needs are simpler:

* **Depth is enforced, not advised.** ``max_depth`` defaults to 1. At the cap
  the tool remains visible and the *call* is refused with an explanation, which
  is what DSH does — hiding the tool would leave the model unable to tell why.
* **Children get their own registry, without the delegation tools.** A child
  therefore cannot reach the parent's sink, and read-only children cannot write.
* **The child's final answer is the tool result.** Nothing else crosses; the
  child's transcript stays in its own run record.
* **A child failure is a tool failure.** It cannot abort the parent.
* **Ran sequentially, not concurrently.** DSH runs up to 8 live children; here
  one at a time is enough and avoids a class of interleaving bugs. Noted as a
  deliberate limit rather than an oversight.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .loop import AgentBudget, AgentLoop, AgentRun
from .protocol import LLMClient
from .tools import ToolRegistry, tool

logger = logging.getLogger(__name__)

# A child gets a smaller allowance than its parent by default: it is answering a
# narrower question, and a child that runs as long as the parent defeats the
# point of delegating.
#
# Sized per round, not per question. At 6 rounds / 30k tokens a real delegated
# question -- "which customer has the strongest evidence of an outstanding
# request, cite the note" -- ran out mid-investigation: 11 tool calls, 29.4k
# tokens, and an empty answer. A truncated child is the worst possible outcome,
# because it spends its whole allowance and returns nothing the parent can use.
# Roughly 5.6k tokens a round still undercuts the parent's 7.5k, so delegating
# remains the cheap way to read a lot.
CHILD_BUDGET = AgentBudget(
    max_rounds=8,
    max_tool_calls=16,
    max_tokens=45_000,
    max_seconds=90.0,
    max_calls_per_round=3,
    context_budget_tokens=12_000,
    keep_recent_messages=4,
)

DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_CHILDREN = 5

# A child must stop *before* the parent's call ceiling, not at the same instant.
# The parent's timer cancels the call, and a cancelled coroutine cannot report;
# it loses the findings it was one step away from returning. This is the margin
# the child gets to unwind and hand back what it has.
DEFAULT_UNWIND_SECONDS = 0.25

CHILD_SYSTEM_PROMPT = """You are a research assistant working on one specific question.

You have tools. Use them to gather the evidence needed to answer, then stop and
report.

How to work:
- The question is narrow on purpose. Answer that question, not the wider one.
- Gather what you need with tools, then reply with your findings as plain text.
  Your text reply ends your work and is returned to the agent that asked you.
- Be concise. The agent that asked you does not want your transcript, it wants
  your conclusion.

Rules:
- Cite the concrete evidence you used — names, numbers, quoted lines, dates.
- Distinguish what you verified from what you are inferring, and say plainly
  what you could not determine. A named gap is a useful result; a confident
  guess is not.
- Never invent data. If a tool returned nothing, report that."""


@dataclass
class Delegation:
    """One child run, recorded so the parent's trace shows what was delegated."""

    question: str
    run: Optional[AgentRun] = None
    answer: str = ""
    ok: bool = False
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "question": self.question,
            "ok": self.ok,
            "error": self.error,
            "answer": self.answer,
            "run_id": self.run.run_id if self.run else None,
            "status": self.run.status if self.run else None,
            "tool_sequence": self.run.tool_sequence if self.run else [],
            "rounds": len(self.run.steps) if self.run else 0,
            "tool_calls": self.run.tool_call_count if self.run else 0,
            "tokens": (
                self.run.prompt_tokens + self.run.completion_tokens if self.run else 0
            ),
            "elapsed": round(self.run.elapsed, 3) if self.run else 0.0,
        }


@dataclass
class DelegationRecord:
    """Every child spawned during one parent run."""

    children: list[Delegation] = field(default_factory=list)
    refused: int = 0

    def _child_runs(self) -> list[AgentRun]:
        return [c.run for c in self.children if c.run is not None]

    def as_dict(self) -> dict:
        """The parent's view, **plus the task totals**.

        A delegated tool call is work this task did, and the parent's own
        counters cannot see it: ``max_tool_calls=25`` bound the parent while its
        children spent another 60 calls and 150k tokens. Reporting only the
        parent's numbers understates what one task costs, so the totals are
        computed here rather than left to be added up by hand.
        """
        runs = self._child_runs()
        return {
            "spawned": len(self.children),
            "refused": self.refused,
            "children": [c.as_dict() for c in self.children],
            "tool_calls": sum(r.tool_call_count for r in runs),
            "rounds": sum(len(r.steps) for r in runs),
            "tokens": sum(r.prompt_tokens + r.completion_tokens for r in runs),
            "elapsed": round(sum(r.elapsed for r in runs), 3),
        }


def build_child_registry(
    parent: ToolRegistry,
    *,
    read_only: bool = True,
    extra_excluded: Optional[set[str]] = None,
) -> ToolRegistry:
    """A registry for a child: the parent's tools minus delegation, and no writes.

    Building a fresh registry rather than sharing the parent's is what keeps a
    child from reaching shared mutable state — a scan's prospect sink being the
    concrete example. A child that could call ``save_prospects`` would be
    writing into the parent's output from a context the parent cannot see.
    """
    excluded = {"spawn_researcher", "recall_observation"}
    excluded |= set(extra_excluded or ())

    child = ToolRegistry(depth=parent.depth + 1)
    for name in parent.names():
        if name in excluded:
            continue
        original = parent.get(name)
        if read_only and "write" in original.tags:
            continue
        child.register(original)
    return child


def build_spawn_tool(
    *,
    llm: LLMClient,
    parent_registry: ToolRegistry,
    record: DelegationRecord,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_children: int = DEFAULT_MAX_CHILDREN,
    child_budget: Optional[AgentBudget] = None,
    read_only: bool = True,
    spill_dir: Optional[Path] = None,
):
    """Build the ``spawn_researcher`` tool bound to one parent run.

    Closure-bound rather than module-level because it needs the model, the
    parent's tools, and the per-run delegation record.
    """
    parent_depth = parent_registry.depth
    @tool(
        name="spawn_researcher",
        description=(
            "Delegate a narrow, self-contained question to a sub-agent that has its "
            "own context. Use it when answering would require reading a lot of "
            "material you do not want to keep: give it the specific question, and it "
            "returns its conclusion rather than everything it read. Ask one clear "
            "question per call. The sub-agent cannot send anything or modify records."
        ),
        tags={"read", "delegation"},
    )
    async def spawn_researcher(
        question: str,
        focus: str = "",
        _deadline: Optional[float] = None,
        _tool_timeout: Optional[float] = None,
    ) -> dict:
        question = (question or "").strip()
        if not question:
            return {"error": "question is required"}

        # Depth: refused at the cap, with the reason, rather than hidden.
        if parent_depth >= max_depth:
            record.refused += 1
            return {
                "error": (
                    f"cannot delegate: already {parent_depth} level(s) deep and the "
                    f"limit is {max_depth}. Answer the question yourself with the "
                    "tools you have."
                ),
                "reason": "max_depth_reached",
            }

        if len(record.children) >= max_children:
            record.refused += 1
            return {
                "error": (
                    f"cannot delegate: this run has already spawned {max_children} "
                    "sub-agents, which is the limit. Use the findings you already have."
                ),
                "reason": "max_children_reached",
            }

        # The task's clock, not this loop's. A child inherits the parent's
        # absolute deadline and gets at most what is left of it, so the coarse
        # statement "a run takes at most max_seconds" stays true no matter how
        # many children it spawns. Before this, each child got a fresh
        # CHILD_BUDGET on its own clock, and five delegations in one round could
        # run 180 + 5 x 90 = 630 seconds for a task advertised as 180.
        #
        # It is also capped by the parent's per-call ceiling, because that is the
        # wall the parent will actually enforce: a child allowed 90s behind a
        # 60s call cap was always cancelled at 60s, losing everything it had
        # found. Given the smaller number, it stops itself and reports.
        allowance = replace(child_budget or CHILD_BUDGET)
        ceilings = [allowance.max_seconds]
        if _tool_timeout is not None and _tool_timeout > 0:
            ceilings.append(_tool_timeout)
        if _deadline is not None:
            remaining = _deadline - time.monotonic()
            if remaining <= 0:
                record.refused += 1
                return {
                    "error": (
                        "cannot delegate: this run's time budget is exhausted. "
                        "Report what you have established so far and say what is "
                        "still unverified."
                    ),
                    "reason": "deadline_reached",
                }
            ceilings.append(remaining)
            allowance.deadline = _deadline
        # Minus the unwind margin, so the child returns something rather than
        # being cancelled at the same instant its ceiling expires.
        allowance.max_seconds = max(0.05, min(ceilings) - DEFAULT_UNWIND_SECONDS)

        child_registry = build_child_registry(parent_registry, read_only=read_only)
        if len(child_registry) == 0:
            record.refused += 1
            return {
                "error": "cannot delegate: there are no read-only tools to give a sub-agent.",
                "reason": "no_child_tools",
            }

        prompt = question if not focus else f"{question}\n\nFocus particularly on: {focus}"
        delegation = Delegation(question=prompt)
        record.children.append(delegation)

        try:
            child = AgentLoop(
                llm,
                child_registry,
                budget=allowance,
                system_prompt=CHILD_SYSTEM_PROMPT,
                spill_dir=spill_dir,
            )
            run = await child.run(prompt)
        except asyncio.CancelledError:
            # The parent's own call ceiling cut this child off mid-flight. Record
            # that, then let the cancellation continue: swallowing it would make
            # the parent's wait_for believe the call had succeeded. Without this
            # the record showed a spawned child with no run and no reason.
            delegation.ok = False
            delegation.error = "cancelled: the parent run's call ceiling was reached"
            raise
        except Exception as exc:  # noqa: BLE001 - a child must not abort the parent
            logger.warning("sub-agent failed: %s", exc, exc_info=True)
            delegation.ok = False
            delegation.error = f"{type(exc).__name__}: {exc}"
            return {
                "error": f"the sub-agent failed: {exc}",
                "note": "Answer the question yourself with the tools you have.",
            }

        delegation.run = run
        answer = (run.final_output or "").strip()

        if run.status != "succeeded" or not answer:
            delegation.ok = False
            delegation.error = f"child run ended {run.status}: {run.stop_reason}"
            return {
                "error": (
                    f"the sub-agent did not reach an answer (it stopped as "
                    f"'{run.status}': {run.stop_reason}). "
                    "Either narrow the question, or answer it yourself."
                ),
                "child_tool_sequence": run.tool_sequence,
                "child_status": run.status,
            }

        delegation.ok = True
        delegation.answer = answer
        return {
            "answer": answer,
            # Provenance only: enough for the parent to judge how much weight to
            # give the answer, without importing the child's whole transcript.
            "how_it_was_established": {
                "tools": run.tool_sequence,
                "rounds": len(run.steps),
                "child_run_id": run.run_id,
            },
        }

    return spawn_researcher


def attach_delegation(
    registry: ToolRegistry,
    *,
    llm: LLMClient,
    record: Optional[DelegationRecord] = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_children: int = DEFAULT_MAX_CHILDREN,
    child_budget: Optional[AgentBudget] = None,
    read_only: bool = True,
    spill_dir: Optional[Path] = None,
) -> tuple[ToolRegistry, DelegationRecord]:
    """Return **a copy** of the registry that can delegate, plus its run record.

    The copy is the point, not an implementation detail. ``spawn_researcher``
    closes over the delegation record for one run, so registering it into a
    registry the caller owns binds that record to that registry forever.
    Attaching twice to the same registry used to hit the duplicate-name guard,
    skip silently, and leave the second run holding a record nothing would ever
    write to — while the first run's record collected the second run's children.

    Returning a fresh registry makes one run's delegation scope impossible to
    share with another by accident::

        registry, record = attach_delegation(base_registry, llm=client)
    """
    record = record or DelegationRecord()
    scoped = registry.copy() if hasattr(registry, "copy") else registry
    scoped.register(
        build_spawn_tool(
            llm=llm,
            parent_registry=scoped,
            record=record,
            max_depth=max_depth,
            max_children=max_children,
            child_budget=child_budget,
            read_only=read_only,
            spill_dir=spill_dir,
        )
    )
    return scoped, record
