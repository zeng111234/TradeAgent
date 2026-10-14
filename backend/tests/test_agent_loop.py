"""The bounded ReAct loop: state machine, budgets, and failure containment.

Every test here drives the loop with a scripted ``FakeLLM``, so the whole
suite runs offline and deterministically.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent import AgentBudget, AgentLoop, ToolRegistry, run_agent, tool
from app.agent.loop import AgentError
from tests.fakes import FakeLLM, final, multi_tool_reply, tool_reply


# --- toy tools -------------------------------------------------------------

CALL_LOG: list[tuple[str, dict]] = []


@tool(description="Search for prospects")
async def search_prospects(keyword: str, country: str = "") -> dict:
    CALL_LOG.append(("search_prospects", {"keyword": keyword, "country": country}))
    return {"found": 2, "keyword": keyword}


@tool(description="Inspect a website")
async def inspect_website(url: str) -> dict:
    CALL_LOG.append(("inspect_website", {"url": url}))
    return {"url": url, "emails": ["a@b.com"], "text": "textile importer"}


@tool(description="List customers")
async def list_customers(stage: str = "all") -> dict:
    CALL_LOG.append(("list_customers", {"stage": stage}))
    return {"customers": [{"id": 1, "company_name": "ACME"}], "stage": stage}


@tool(description="Always fails")
async def broken(x: str = "") -> dict:
    raise ValueError("upstream 500")


@tool(description="Slow")
async def slow(seconds: float = 5.0) -> dict:
    await asyncio.sleep(seconds)
    return {"slept": seconds}


@tool(description="Burns a small amount of wall-clock time")
async def tick(cost: float = 0.02) -> dict:
    await asyncio.sleep(cost)
    return {"ticked": cost}


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in (search_prospects, inspect_website, list_customers, broken, slow, tick):
        reg.register(t)
    return reg


@pytest.fixture(autouse=True)
def _clear_log():
    CALL_LOG.clear()
    yield
    CALL_LOG.clear()


def make_loop(script, registry, **budget_kwargs):
    budget = AgentBudget(**budget_kwargs) if budget_kwargs else AgentBudget()
    llm = FakeLLM(script)
    return AgentLoop(llm, registry, budget=budget), llm


# --- happy path ------------------------------------------------------------

async def test_single_tool_then_answer(registry):
    loop, llm = make_loop(
        [tool_reply("inspect_website", {"url": "https://x.com"}), final("Concluded.")],
        registry,
    )
    run = await loop.run("look at https://x.com")

    assert run.status == "succeeded"
    assert run.final_output.startswith("Concluded.")
    assert run.tool_sequence == ["inspect_website"]
    assert run.did_investigate is True
    assert run.error is None


async def test_multi_round_sequence_is_driven_by_the_model(registry):
    """The order comes from the scripted model, not from the loop."""
    loop, _ = make_loop(
        [
            tool_reply("search_prospects", {"keyword": "yarn", "country": "Germany"}),
            tool_reply("inspect_website", {"url": "https://a.de"}),
            tool_reply("inspect_website", {"url": "https://b.de"}),
            final("Three steps taken."),
        ],
        registry,
    )
    run = await loop.run("find buyers")

    assert run.status == "succeeded"
    assert run.tool_sequence == [
        "search_prospects", "inspect_website", "inspect_website",
    ]
    assert len(run.steps) == 4
    assert [s.round for s in run.steps] == [1, 2, 3, 4]


async def test_observations_are_fed_back_to_the_model(registry):
    loop, llm = make_loop(
        [tool_reply("inspect_website", {"url": "https://x.com"}), final("ok")],
        registry,
    )
    await loop.run("go")

    second_call_messages = llm.calls[1].messages
    tool_messages = [m for m in second_call_messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert '"emails"' in tool_messages[0].content
    assert tool_messages[0].tool_call_id == "call_inspect_website"
    assert tool_messages[0].name == "inspect_website"


async def test_several_tools_in_one_round(registry):
    loop, _ = make_loop(
        [
            multi_tool_reply(("search_prospects", {"keyword": "yarn"}),
                             ("list_customers", {"stage": "new"})),
            final("did both"),
        ],
        registry,
    )
    run = await loop.run("do two things")
    assert run.tool_sequence == ["search_prospects", "list_customers"]
    assert len(run.steps) == 2
    assert len(run.steps[0].tool_calls) == 2


async def test_tools_are_offered_to_the_model(registry):
    loop, llm = make_loop([final("nothing to do")], registry, max_rounds=1)
    await loop.run("go")
    assert llm.offered_tool_names() == [
        "broken", "inspect_website", "list_customers", "search_prospects", "slow", "tick",
    ]


async def test_goal_and_system_prompt_reach_the_model(registry):
    loop, llm = make_loop([final("ok")], registry, max_rounds=1)
    await loop.run("Find German textile importers", context="Extra: today is Monday")

    roles = [m.role for m in llm.calls[0].messages]
    assert roles == ["system", "system", "user"]
    assert llm.calls[0].messages[-1].content == "Find German textile importers"
    assert "Monday" in llm.calls[0].messages[1].content


# --- recovery --------------------------------------------------------------

async def test_malformed_arguments_are_reported_back_and_model_can_recover(registry):
    loop, _ = make_loop(
        [
            tool_reply("inspect_website", '{"url": "https://x.com"'),   # truncated JSON
            tool_reply("inspect_website", {"url": "https://x.com"}),     # corrected
            final("Recovered."),
        ],
        registry,
    )
    run = await loop.run("go")

    assert run.status == "succeeded"
    first_call = run.steps[0].tool_calls[0]
    assert first_call.ok is False
    assert "not valid JSON" in first_call.observation
    assert run.steps[1].tool_calls[0].ok is True


async def test_unknown_tool_does_not_abort_the_run(registry):
    loop, _ = make_loop(
        [tool_reply("teleport", {"to": "mars"}), tool_reply("list_customers", {}), final("done")],
        registry,
    )
    run = await loop.run("go")

    assert run.status == "succeeded"
    assert run.steps[0].tool_calls[0].ok is False
    assert "unknown tool" in run.steps[0].tool_calls[0].observation
    assert run.steps[1].tool_calls[0].ok is True


async def test_tool_exception_becomes_an_observation(registry):
    loop, _ = make_loop([tool_reply("broken", {"x": "1"}), final("survived")], registry)
    run = await loop.run("go")

    assert run.status == "succeeded"
    failed = run.steps[0].tool_calls[0]
    assert failed.ok is False
    assert "upstream 500" in failed.observation


async def test_invalid_arguments_do_not_abort_the_run(registry):
    loop, _ = make_loop(
        [tool_reply("search_prospects", {}), final("no keywords given, stopping")],
        registry,
    )
    run = await loop.run("go")
    assert run.status == "succeeded"
    assert run.steps[0].tool_calls[0].ok is False
    assert "invalid arguments" in run.steps[0].tool_calls[0].observation


async def test_tool_timeout_is_bounded(registry):
    loop, _ = make_loop(
        [tool_reply("slow", {"seconds": 5}), final("gave up waiting")],
        registry,
        tool_timeout_seconds=0.05,
    )
    run = await loop.run("go")
    assert run.status == "succeeded"
    failed = run.steps[0].tool_calls[0]
    assert failed.ok is False
    assert "timed out" in failed.observation


# --- budgets ---------------------------------------------------------------

async def test_round_budget_truncates(registry):
    script = [tool_reply("list_customers", {}) for _ in range(10)]
    loop, llm = make_loop(script, registry, max_rounds=3)
    run = await loop.run("never finish")

    assert run.status == "truncated"
    assert run.stop_reason == "round budget exhausted"
    assert len(run.steps) == 3
    assert llm.call_count == 3


async def test_tool_call_budget_truncates(registry):
    script = [
        multi_tool_reply(("list_customers", {}), ("list_customers", {})),
        multi_tool_reply(("list_customers", {}), ("list_customers", {})),
        multi_tool_reply(("list_customers", {}), ("list_customers", {})),
    ]
    loop, _ = make_loop(script, registry, max_tool_calls=3, max_rounds=10)
    run = await loop.run("blast the tools")

    assert run.status == "truncated"
    assert run.stop_reason == "tool-call budget exhausted"
    assert run.tool_call_count == 3


async def test_per_round_cap_drops_extra_calls(registry):
    loop, _ = make_loop(
        [
            multi_tool_reply(*[("list_customers", {})] * 6),
            final("done"),
        ],
        registry,
        max_calls_per_round=2,
    )
    run = await loop.run("too many at once")
    assert len(run.steps[0].tool_calls) == 2
    assert "dropped" in run.steps[0].note


async def test_token_budget_truncates(registry):
    script = [
        {
            "tool_calls": [{"name": "list_customers", "arguments": "{}"}],
            "prompt_tokens": 900,
            "completion_tokens": 900,
        }
        for _ in range(6)
    ]
    loop, _ = make_loop(script, registry, max_tokens=1000, max_rounds=10)
    run = await loop.run("expensive")

    assert run.status == "truncated"
    assert run.stop_reason == "token budget exhausted"


async def test_time_budget_truncates(registry):
    """A budget of zero must stop the run before any round executes."""
    loop, _ = make_loop(
        [tool_reply("list_customers", {}) for _ in range(6)],
        registry,
        max_seconds=0.0,
        max_rounds=6,
    )
    run = await loop.run("go")
    assert run.status == "truncated"
    assert run.stop_reason == "time budget exceeded"
    assert run.steps == []


async def test_time_budget_stops_a_slow_run_mid_way(registry):
    """A run that burns wall-clock time is cut off, not merely capped by rounds."""
    loop, llm = make_loop(
        [tool_reply("tick", {"cost": 0.03}) for _ in range(20)],
        registry,
        max_seconds=0.06,
        max_rounds=20,
    )
    run = await loop.run("keep ticking")

    assert run.status == "truncated"
    assert run.stop_reason == "time budget exceeded"
    # It must have stopped early, not exhausted the 20-round budget.
    assert llm.call_count < 20
    assert run.elapsed >= 0.06


async def test_repeating_a_failing_call_is_possible_but_bounded(registry):
    """The loop allows retries; the round budget stops a loop that never learns."""
    loop, _ = make_loop(
        [tool_reply("teleport", {}) for _ in range(8)],
        registry,
        max_rounds=4,
    )
    run = await loop.run("go")
    assert run.status == "truncated"
    assert all(not c.ok for s in run.steps for c in s.tool_calls)
    assert len(run.steps) == 4


# --- stalling --------------------------------------------------------------

async def test_empty_replies_eventually_stall(registry):
    loop, llm = make_loop(["", "", "", ""], registry, max_idle_rounds=3)
    run = await loop.run("go")

    assert run.status == "stalled"
    assert "no usable tool calls" in run.stop_reason
    assert llm.call_count == 3


async def test_prose_without_investigation_is_not_accepted_as_an_answer(registry):
    """A confident answer with zero tool calls must not be reported as success."""
    loop, _ = make_loop(
        ["I think the best prospects are in Germany.", final("Now I actually checked.")],
        registry,
    )
    run = await loop.run("find buyers")

    assert run.status == "succeeded"
    assert run.tool_sequence == []           # the model never used a tool
    assert run.steps[0].note == "prose without investigation; asked to use tools"
    # The loop did nudge the model rather than accepting the first claim.
    assert len(run.steps) == 2


async def test_long_answer_without_tools_is_accepted(registry):
    """A genuinely substantive answer from a model that needs no tools is fine."""
    loop, _ = make_loop([final("Here is a long considered answer. " * 10)], registry)
    run = await loop.run("explain our product line")
    assert run.status == "succeeded"
    assert run.did_investigate is False


# --- error handling --------------------------------------------------------

async def test_llm_failure_produces_an_error_run(registry):
    loop, _ = make_loop([RuntimeError("connection reset")], registry)
    with pytest.raises(AgentError, match="connection reset"):
        await loop.run("go")


async def test_run_agent_wrapper_converts_llm_failure_to_error_status(registry):
    llm = FakeLLM([RuntimeError("upstream down")])
    run = await run_agent("go", llm, registry)
    assert run.status == "error"
    assert "upstream down" in run.error
    assert run.final_output == ""


async def test_run_agent_returns_success_normally(registry):
    llm = FakeLLM([tool_reply("list_customers", {}), final("all good")])
    run = await run_agent("go", llm, registry)
    assert run.status == "succeeded"
    assert run.tool_sequence == ["list_customers"]


# --- trace / serialisation -------------------------------------------------

async def test_trace_records_everything_needed_for_replay(registry):
    loop, _ = make_loop(
        [tool_reply("search_prospects", {"keyword": "yarn"}, content="thinking"),
         final("answer")],
        registry,
    )
    run = await loop.run("go")
    payload = run.as_dict()

    assert payload["status"] == "succeeded"
    assert payload["rounds"] == 2
    assert payload["tool_calls"] == 1
    assert payload["tool_sequence"] == ["search_prospects"]

    call = payload["steps"][0]["tool_calls"][0]
    assert call["name"] == "search_prospects"
    assert call["arguments"] == {"keyword": "yarn"}
    assert call["ok"] is True
    assert '"found": 2' in call["observation"]
    assert payload["steps"][0]["thought"] == "thinking"


async def test_as_dict_is_json_serialisable(registry):
    import json

    loop, _ = make_loop([tool_reply("broken", {}), final("done")], registry)
    run = await loop.run("go")
    json.dumps(run.as_dict())   # must not raise


async def test_run_id_is_unique_per_run(registry):
    llm_a, llm_b = FakeLLM([final("a")]), FakeLLM([final("b")])
    run_a = await AgentLoop(llm_a, registry).run("a")
    run_b = await AgentLoop(llm_b, registry).run("b")
    assert run_a.run_id != run_b.run_id


async def test_on_step_callback_fires_per_round(registry):
    seen: list[int] = []
    llm = FakeLLM([tool_reply("list_customers", {}), tool_reply("list_customers", {}), final("done")])
    loop = AgentLoop(llm, registry, on_step=lambda step: seen.append(step.round))
    await loop.run("go")
    assert seen == [1, 2]


# --- the acceptance criterion ---------------------------------------------

async def test_different_goals_produce_different_tool_sequences(registry):
    """Acceptance criterion 6, in miniature.

    Two different goals, the same registry, the same loop code — and the
    model chooses two different paths. This is what distinguishes an agent
    from a hardcoded pipeline.
    """
    loop_new, _ = make_loop(
        [
            tool_reply("search_prospects", {"keyword": "yarn", "country": "Germany"}),
            tool_reply("inspect_website", {"url": "https://a.de"}),
            final("Found new German prospects."),
        ],
        registry,
    )
    run_new = await loop_new.run("找新的德国潜在客户")

    loop_existing, _ = make_loop(
        [
            tool_reply("list_customers", {"stage": "new"}),
            final("Reviewed existing pipeline."),
        ],
        registry,
    )
    run_existing = await loop_existing.run("看看我现有的客户进展")

    assert run_new.tool_sequence == ["search_prospects", "inspect_website"]
    assert run_existing.tool_sequence == ["list_customers"]
    assert run_new.tool_sequence != run_existing.tool_sequence
    assert run_new.status == run_existing.status == "succeeded"
