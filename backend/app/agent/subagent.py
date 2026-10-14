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

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .loop import AgentBudget, AgentLoop, AgentRun
from .protocol import LLMClient
from .tools import ToolRegistry, tool

logger = logging.getLogger(__name__)

# A child gets a smaller allowance than its parent by default: it is answering a
# narrower question, and a child that runs as long as the parent defeats the
# point of delegating.
CHILD_BUDGET = AgentBudget(
    max_rounds=6,
    max_tool_calls=12,
    max_tokens=30_000,
    max_seconds=90.0,
    max_calls_per_round=3,
    context_budget_tokens=12_000,
    keep_recent_messages=4,
)

DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_CHILDREN = 5

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
        }


@dataclass
class DelegationRecord:
    """Every child spawned during one parent run."""

    children: list[Delegation] = field(default_factory=list)
    refused: int = 0

    def as_dict(self) -> dict:
        return {
            "spawned": len(self.children),
            "refused": self.refused,
            "children": [c.as_dict() for c in self.children],
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
    budget = child_budget or CHILD_BUDGET
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
    async def spawn_researcher(question: str, focus: str = "") -> dict:
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
                budget=budget,
                system_prompt=CHILD_SYSTEM_PROMPT,
                spill_dir=spill_dir,
            )
            run = await child.run(prompt)
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
) -> DelegationRecord:
    """Give a registry the ability to delegate. Returns the run's record."""
    record = record or DelegationRecord()
    if "spawn_researcher" not in registry:
        registry.register(
            build_spawn_tool(
                llm=llm,
                parent_registry=registry,
                record=record,
                max_depth=max_depth,
                max_children=max_children,
                child_budget=child_budget,
                read_only=read_only,
                spill_dir=spill_dir,
            )
        )
    return record
