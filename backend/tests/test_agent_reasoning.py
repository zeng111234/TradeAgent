"""The trace has to say what the model actually did -- including thinking.

Three claims, each of which was false before this file existed:

* a provider's reasoning channel is recorded, not dropped;
* a round's latency is measured, not a constant zero;
* a missing usage block is reported as *unknown*, not as a free run.

The fourth is a negative: reasoning is never replayed to the model. Providers
either reject that or require a signature this runtime cannot mint.
"""
from __future__ import annotations

import asyncio

from app.agent import AgentBudget, AgentLoop, ToolRegistry, tool
from app.agent.loop import MAX_REASONING_CHARS
from app.agent.protocol import LLMReply, ToolCall
from tests.fakes import FakeLLM, final


@tool(description="Look something up")
async def lookup(keyword: str) -> dict:
    return {"keyword": keyword}


def build_loop(script, **budget_kwargs):
    registry = ToolRegistry()
    registry.register(lookup)
    budget = AgentBudget(**budget_kwargs) if budget_kwargs else AgentBudget()
    return AgentLoop(FakeLLM(script), registry, budget=budget)


async def test_reasoning_is_recorded_on_the_step():
    loop = build_loop(
        [
            LLMReply(
                content="",
                reasoning="The goal names a country; I should search first.",
                tool_calls=[ToolCall(id="c1", name="lookup", arguments='{"keyword": "yarn"}')],
                prompt_tokens=11,
                completion_tokens=7,
            ),
            final("Done."),
        ]
    )

    run = await loop.run("find yarn buyers in Germany")

    first = run.steps[0]
    assert first.reasoning.startswith("The goal names a country")
    assert first.thought == ""
    assert first.reasoning_truncated is False
    assert run.steps[0].as_dict()["reasoning"].startswith("The goal names")


async def test_reasoning_is_never_replayed_to_the_model():
    """Recording thinking is not the same as sending it back."""
    llm = FakeLLM(
        [
            LLMReply(
                content="",
                reasoning="SECRET-CHAIN-OF-THOUGHT",
                tool_calls=[ToolCall(id="c1", name="lookup", arguments='{"keyword": "yarn"}')],
            ),
            final("Done."),
        ]
    )
    loop = AgentLoop(llm, _registry_with_lookup(), budget=AgentBudget())

    await loop.run("find yarn buyers")

    sent = "\n".join(message.content or "" for message in llm.last_messages())
    assert "SECRET-CHAIN-OF-THOUGHT" not in sent


async def test_a_long_reasoning_is_truncated_and_flagged():
    loop = build_loop(
        [
            LLMReply(content="", reasoning="x" * (MAX_REASONING_CHARS + 50)),
            final("Done."),
        ]
    )

    run = await loop.run("think for a long time")

    step = run.steps[0]
    assert len(step.reasoning) == MAX_REASONING_CHARS
    assert step.reasoning_truncated is True


async def test_latency_is_measured_around_the_model_call():
    """It used to be a field that was never passed and rendered as `0`."""

    class SlowLLM:
        model = "fake-model"

        async def chat(self, messages, tools, *, temperature=0.2, max_tokens=1500):
            # 50ms, not 20: Windows' timer granularity is ~15.6ms, so a sleep
            # this short can return a hair early and make the assertion below
            # fail on the clock rather than on the code.
            await asyncio.sleep(0.05)
            return LLMReply(
                content=final("Slow but done."),
                prompt_tokens=5,
                completion_tokens=5,
            )

    loop = AgentLoop(SlowLLM(), _registry_with_lookup(), budget=AgentBudget())
    run = await loop.run("take your time")

    assert run.steps[0].latency >= 0.03
    assert run.steps[0].as_dict()["latency"] > 0


async def test_the_model_that_ran_is_recorded():
    class NamedLLM(FakeLLM):
        model = "fake-model-42"

    llm = NamedLLM([final("Done.")])
    loop = AgentLoop(llm, _registry_with_lookup(), budget=AgentBudget())

    run = await loop.run("answer briefly")

    assert run.model == "fake-model-42"
    assert run.as_dict()["model"] == "fake-model-42"


async def test_a_missing_usage_block_is_unknown_not_zero():
    loop = build_loop(
        [
            LLMReply(
                content=final("Answered."),
                prompt_tokens=0,
                completion_tokens=0,
                usage_reported=False,
            )
        ]
    )

    run = await loop.run("answer briefly")

    assert run.steps[0].usage_missing is True
    assert run.steps[0].as_dict()["usage_missing"] is True
    assert run.usage_complete is False
    assert run.as_dict()["usage_complete"] is False


def _registry_with_lookup() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(lookup)
    return registry
