"""Where one task ends.

A "task" is one ``AgentLoop.run()`` plus every sub-agent it spawns. The point of
these tests is that the task's boundary is *not* the loop's boundary: each loop
has its own ceilings, and a loop that delegates starts more of them.

Measured before the fix, with a one-second parent budget and two delegations in
a single round::

    parent max_seconds                 : 1.0
    parent run.elapsed                 : 2.42s
    OVERRUN                            : +1.42s
    parent tool_call_count (vs cap 25) : 2
    child tool_calls, NOT in that count: 2
    parent tokens (vs cap 100000)      : 230
    child tokens, NOT in that count    : 920

Scaled to the presets in ``service.py`` -- 180s parent, 90s child, 5 children per
round -- the same composition rule gives 630 seconds for a task the API promised
would take at most 180. So ``max_seconds`` now means the task, not the loop.
"""
from __future__ import annotations

import time

import pytest

from app.agent.loop import AgentBudget, AgentLoop
from app.agent.subagent import CHILD_BUDGET, attach_delegation
from app.agent.tools import ToolRegistry, tool
from tests.scripted_llm import ScriptedLLM, call, final

# Scheduling noise. Assertions use this as a tolerance and it is deliberately
# generous: the property is "the budget is a bound", not "the clock is exact".
SLACK = 0.15


@tool(description="Sleep, then report")
async def slow_read(seconds: float = 1.0) -> dict:
    import asyncio

    await asyncio.sleep(seconds)
    return {"read": True}


@tool(description="Block the event loop, the way a synchronous call would")
async def blocks_for(seconds: float = 0.5) -> dict:
    # Deliberately synchronous: the loop cannot time this out, so it overruns
    # the deadline by a known margin instead of racing it.
    time.sleep(seconds)
    return {"blocked": seconds}


def two_spawns() -> dict:
    """One assistant turn asking for two delegations: one round, two children.

    This is the shape that made the boundary leak, because a round is charged
    against the parent's budget only once, at its start.
    """
    return {
        "content": "",
        "tool_calls": [
            {"id": "spawn_1", "name": "spawn_researcher",
             "arguments": {"question": "what is X"}},
            {"id": "spawn_2", "name": "spawn_researcher",
             "arguments": {"question": "what is Y"}},
        ],
    }


def build(script, *, parent: AgentBudget, child: AgentBudget | None = None):
    llm = ScriptedLLM(script)
    base = ToolRegistry()
    base.register(slow_read)
    registry, record = attach_delegation(base, llm=llm, child_budget=child)
    loop = AgentLoop(llm, registry, budget=parent)
    return loop, record


# --------------------------------------------------------------------------
# The bound holds across delegation
# --------------------------------------------------------------------------

async def test_a_delegating_run_does_not_outlive_its_own_time_budget():
    budget = AgentBudget(max_rounds=4, max_seconds=0.5, max_tool_calls=25,
                         max_tokens=100_000, tool_timeout_seconds=5.0)
    child = AgentBudget(max_rounds=3, max_seconds=10.0, max_tool_calls=10,
                        max_tokens=100_000)
    loop, record = build(
        [two_spawns(), call("slow_read", {"seconds": 5.0}), final("child answer"),
         final("parent conclusion")],
        parent=budget, child=child,
    )

    run = await loop.run("do the task")

    assert run.elapsed <= budget.max_seconds + SLACK, (
        f"a task advertised as {budget.max_seconds}s ran {run.elapsed:.2f}s"
    )
    assert record.children, "the delegations should have been attempted"
    assert run.status in ("succeeded", "truncated")


async def test_a_child_allowance_is_clipped_to_what_the_parent_has_left():
    """The child's own 10s ceiling must not survive a 0.4s parent."""
    budget = AgentBudget(max_rounds=4, max_seconds=0.4, max_tool_calls=25,
                         max_tokens=100_000, tool_timeout_seconds=5.0)
    loop, record = build(
        [call("spawn_researcher", {"question": "X"}),
         call("slow_read", {"seconds": 5.0}), final("child answer"),
         final("parent conclusion")],
        parent=budget, child=AgentBudget(max_rounds=3, max_seconds=10.0,
                                         max_tool_calls=10, max_tokens=100_000),
    )

    run = await loop.run("do the task")

    assert run.elapsed <= budget.max_seconds + SLACK
    # The child did not get to use its own 10 seconds.
    for child_record in record.children:
        if child_record.run is not None:
            assert child_record.run.elapsed <= budget.max_seconds + SLACK


async def test_a_refused_delegation_says_the_time_ran_out_rather_than_failing_oddly():
    """A call left in a round after the budget is gone is refused, with a reason."""
    budget = AgentBudget(max_rounds=4, max_seconds=0.3, max_tool_calls=25,
                         max_tokens=100_000, tool_timeout_seconds=60.0)
    script = [
        {
            "content": "",
            "tool_calls": [
                # Blocks past the 0.3s deadline without the loop being able to
                # time it out, so the next call is unambiguously out of time.
                {"id": "block_1", "name": "blocks_for", "arguments": {"seconds": 0.5}},
                {"id": "spawn_1", "name": "spawn_researcher",
                 "arguments": {"question": "X"}},
            ],
        },
        final("parent conclusion"),
    ]
    base = ToolRegistry()
    base.register(slow_read)
    base.register(blocks_for)
    llm = ScriptedLLM(script)
    registry, record = attach_delegation(base, llm=llm,
                                        child_budget=AgentBudget(max_rounds=3))
    loop = AgentLoop(llm, registry, budget=budget)

    run = await loop.run("do the task")

    reasons = [
        c.refused_reason
        for step in run.steps for c in step.tool_calls if c.refused
    ]
    assert "run_deadline_reached" in reasons, (
        f"the spawn after the blocking read should be refused for time, got {reasons}"
    )
    # It is recorded, but it is not work.
    assert run.tool_call_count == 1
    assert run.refused_call_count == 1
    assert record.children == [], "a refused spawn must not appear as a child"


async def test_a_spawn_with_no_time_left_is_refused_and_reported_as_a_failure():
    """Two properties at once: the reason, and ``ok=False`` in the trace.

    A tool that returns ``{"error": ...}`` used to be recorded as a *successful*
    call, because the wrapper marked any returned value ok. The trace then showed
    a call that worked when the tool had deliberately done nothing.
    """
    from app.agent.subagent import DelegationRecord, build_spawn_tool

    base = ToolRegistry()
    base.register(slow_read)
    record = DelegationRecord()
    spawn = build_spawn_tool(llm=ScriptedLLM([]), parent_registry=base, record=record)
    registry = ToolRegistry()
    registry.register(spawn)

    result, _ = await registry.call(
        "spawn_researcher",
        {"question": "X"},
        host={"_deadline": time.monotonic() - 1.0},
    )

    assert result.ok is False
    assert result.raw["reason"] == "deadline_reached"
    assert record.children == []
    assert record.refused == 1


# --------------------------------------------------------------------------
# A child is capped by the parent's call ceiling, so it can report instead of
# being cancelled with nothing to show
# --------------------------------------------------------------------------

async def test_a_child_is_capped_by_the_parents_call_ceiling():
    """90s of allowance behind a 0.4s call cap used to mean cancellation.

    Cancellation loses everything the child had found. Given the smaller
    ceiling, the child stops itself and its own record survives -- which is the
    property being asserted, not any particular terminal status: how far the
    child gets in 0.15s is not the point, that it leaves a record is.
    """
    budget = AgentBudget(max_rounds=4, max_seconds=30.0, max_tool_calls=25,
                         max_tokens=100_000, tool_timeout_seconds=0.4)
    loop, record = build(
        [call("spawn_researcher", {"question": "X"}),
         call("slow_read", {"seconds": 5.0}), final("child answer"),
         final("parent conclusion")],
        parent=budget, child=AgentBudget(max_rounds=3, max_seconds=90.0,
                                         max_tool_calls=10, max_tokens=100_000),
    )

    run = await loop.run("do the task")

    assert len(record.children) == 1
    child = record.children[0]
    assert child.run is not None, (
        f"the child was cancelled instead of stopping itself: {child.error!r}"
    )
    assert "cancel" not in (child.error or "").lower()
    # Its own 90s ceiling was never in play.
    assert child.run.elapsed <= budget.tool_timeout_seconds + SLACK
    # It reached a terminal state under its own power, and did some work first.
    assert child.run.status in ("truncated", "succeeded", "stalled")
    if child.run.status == "truncated":
        assert child.run.stop_reason == "time budget exceeded"
    # And the parent still got a round to report rather than an empty answer.
    assert run.tool_call_count >= 1


async def test_a_cancelled_child_is_recorded_as_cancelled():
    """If cancellation happens, the record must not be blank.

    Before this, a cancelled child left ``ok=False`` with ``error=''`` and no
    run: ``spawned: 1`` with nothing to explain it. Cancellation is real -- it is
    what the parent's call ceiling does to a child that overruns it -- and the
    child must still say so on its way out.
    """
    import asyncio

    from app.agent.subagent import DelegationRecord, build_spawn_tool

    @tool(description="Never returns")
    async def hangs_forever() -> dict:
        await asyncio.sleep(3600)
        return {"never": True}

    base = ToolRegistry()
    base.register(hangs_forever)
    record = DelegationRecord()
    spawn = build_spawn_tool(
        llm=ScriptedLLM([call("hangs_forever", {})]),
        parent_registry=base,
        record=record,
    )

    task = asyncio.create_task(spawn(question="what is X", _deadline=time.monotonic() + 30))
    await asyncio.sleep(0.05)          # let the child reach its hanging tool
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(record.children) == 1
    child = record.children[0]
    assert child.ok is False
    assert child.error, "a cancelled child must say why it has no result"
    assert "cancel" in child.error.lower()
    assert record.as_dict()["spawned"] == 1


# --------------------------------------------------------------------------
# The task total, which the parent's own counters cannot show
# --------------------------------------------------------------------------

async def test_the_record_reports_delegated_work_as_part_of_the_task():
    budget = AgentBudget(max_rounds=4, max_seconds=20.0, max_tool_calls=25,
                         max_tokens=100_000, tool_timeout_seconds=5.0)
    loop, record = build(
        [call("spawn_researcher", {"question": "X"}),
         call("slow_read", {"seconds": 0.0}), final("child answer"),
         final("parent conclusion")],
        parent=budget, child=AgentBudget(max_rounds=3, max_seconds=10.0,
                                         max_tool_calls=10, max_tokens=100_000),
    )

    run = await loop.run("do the task")
    totals = record.as_dict()

    assert totals["spawned"] == 1
    assert totals["tool_calls"] == 1, "the child's read is part of this task"
    assert totals["tokens"] > 0
    # The parent's own counter cannot see it: that is the whole point.
    assert run.tool_call_count == 1          # just the spawn
    assert totals["tool_calls"] not in (0, None)


async def test_summarise_reports_both_the_loops_numbers_and_the_tasks():
    from app.agent.service import AgentResult, summarise

    budget = AgentBudget(max_rounds=4, max_seconds=20.0, max_tool_calls=25,
                         max_tokens=100_000, tool_timeout_seconds=5.0)
    loop, record = build(
        [call("spawn_researcher", {"question": "X"}),
         call("slow_read", {"seconds": 0.0}), final("child answer"),
         final("parent conclusion")],
        parent=budget, child=AgentBudget(max_rounds=3, max_seconds=10.0,
                                         max_tool_calls=10, max_tokens=100_000),
    )
    run = await loop.run("do the task")

    summary = summarise(AgentResult(
        run=run, mermaid_sequence="", mermaid_flowchart="", html="",
        delegations=record.as_dict(),
    ))

    assert summary["tool_calls"] == 1                 # the parent loop
    assert summary["delegated_tool_calls"] == 1       # the child
    assert summary["tool_calls_total"] == 2           # the task
    assert summary["tokens_total"] > summary["prompt_tokens"] + summary["completion_tokens"]
    assert summary["delegations"] == 1


# --------------------------------------------------------------------------
# The deadline is host-injected, so the model cannot extend its own task
# --------------------------------------------------------------------------

def test_the_daylight_between_model_arguments_and_host_arguments():
    """``spawn_researcher``'s schema must not show the deadline."""
    registry, _ = attach_delegation(ToolRegistry(), llm=ScriptedLLM([]))
    spawn = registry.get("spawn_researcher")

    assert spawn.host_params == frozenset({"_deadline", "_tool_timeout"})
    properties = set(spawn.schema()["function"]["parameters"]["properties"])
    assert properties == {"question", "focus"}

    # And the model cannot set it: an extra key is not an accepted argument.
    import asyncio

    from app.agent.tools import ToolRegistry as _Registry

    seen: list[float] = []

    @tool(description="Records the deadline it was given")
    async def probe(_deadline: float = -1.0) -> dict:
        seen.append(_deadline)
        return {"ok": True}

    registry = _Registry()
    registry.register(probe)
    asyncio.run(registry.call("probe", {"_deadline": 999.0}))
    assert seen == [-1.0], "the model's copy of the deadline must be ignored"

    asyncio.run(registry.call("probe", {}, host={"_deadline": 123.0}))
    assert seen == [-1.0, 123.0], "the host's copy must reach the tool"


# --------------------------------------------------------------------------
# A tool cannot outlive the run either
# --------------------------------------------------------------------------

async def test_a_tool_timeout_is_clamped_to_the_run_deadline():
    """A 60s tool with 0.3s left must not run for 60s."""
    budget = AgentBudget(max_rounds=2, max_seconds=0.3, max_tool_calls=5,
                         max_tokens=100_000, tool_timeout_seconds=60.0)
    loop, _ = build(
        [call("slow_read", {"seconds": 5.0}) for _ in range(3)],
        parent=budget,
    )

    started = time.monotonic()
    run = await loop.run("go")
    elapsed = time.monotonic() - started

    assert elapsed <= budget.max_seconds + SLACK
    failure = run.steps[0].tool_calls[0].observation
    assert "time budget ran out" in failure, failure
    assert "timed out after 60" not in failure


# --------------------------------------------------------------------------
# The page a human reads must not understate the task either
# --------------------------------------------------------------------------

def test_the_trace_header_says_the_sub_agents_are_not_in_the_counts():
    from app.agent.render import derive_stats, render_html

    steps = [
        {"round": 1, "tool_calls": [
            {"name": "spawn_researcher", "ok": True, "refused_reason": "",
             "observation": "{}", "arguments": {}},
            {"name": "list_customers", "ok": True, "refused_reason": "",
             "observation": "{}", "arguments": {}},
        ]},
    ]
    stats = derive_stats(steps)
    assert stats["delegated"] == 1
    assert stats["tool_calls"] == 2          # the parent's own calls

    page = render_html({
        "goal": "g", "status": "succeeded", "stop_reason": "final answer",
        "final_output": "done", "steps": steps,
        **stats, "elapsed": 1.0, "prompt_tokens": 10, "completion_tokens": 5,
    })
    assert "sub-agents" in page
    assert "not in the counts above" in page


def test_the_trace_header_is_quiet_when_nothing_was_delegated():
    from app.agent.render import derive_stats, render_html

    steps = [{"round": 1, "tool_calls": [
        {"name": "list_customers", "ok": True, "refused_reason": "",
         "observation": "{}", "arguments": {}},
    ]}]
    stats = derive_stats(steps)
    assert stats["delegated"] == 0

    page = render_html({
        "goal": "g", "status": "succeeded", "stop_reason": "final answer",
        "final_output": "done", "steps": steps,
        **stats, "elapsed": 1.0, "prompt_tokens": 10, "completion_tokens": 5,
    })
    assert "sub-agents" not in page
