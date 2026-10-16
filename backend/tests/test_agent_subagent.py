"""Sub-agents: delegation with context isolation.

The property under test is not "a child ran" but "the child's working stayed out
of the parent's context, and the parent still got a usable answer".
"""
from __future__ import annotations

import pytest

from app.agent.loop import AgentBudget, AgentLoop
from app.agent.subagent import (
    CHILD_BUDGET,
    DelegationRecord,
    attach_delegation,
    build_child_registry,
)
from app.agent.tools import ToolRegistry, tool
from tests.scripted_llm import ScriptedLLM, call, final


@tool(description="Read something big", tags={"read", "prospect"})
async def read_big(url: str) -> dict:
    return {"url": url, "text": "PAGE TEXT " * 400}


@tool(description="List customers", tags={"read", "crm"})
async def list_customers(stage: str = "all") -> dict:
    return {"stage": stage, "customers": [{"id": 1, "company_name": "ACME"}]}


@tool(description="Add a customer", tags={"write", "crm"})
async def upsert_customer(company_name: str) -> dict:
    return {"status": "created", "customer_id": 7}


@tool(description="Save prospects", tags={"write", "scan"})
async def save_prospects(prospects: list[dict]) -> dict:
    return {"added": len(prospects)}


def parent_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for entry in (read_big, list_customers, upsert_customer, save_prospects):
        registry.register(entry)
    return registry


# --- the child's registry --------------------------------------------------

def test_child_registry_excludes_write_tools():
    """A child writing into state its parent cannot see is the failure mode."""
    child = build_child_registry(parent_registry(), read_only=True)
    assert "upsert_customer" not in child
    assert "save_prospects" not in child
    assert "read_big" in child
    assert "list_customers" in child


def test_child_registry_can_be_given_write_tools_explicitly():
    child = build_child_registry(parent_registry(), read_only=False)
    assert "upsert_customer" in child


def test_child_registry_never_inherits_delegation_tools():
    """Otherwise a child could recurse past the depth check."""
    parent = parent_registry()
    parent, _ = attach_delegation(parent, llm=ScriptedLLM([]), max_depth=5)
    assert "spawn_researcher" in parent

    child = build_child_registry(parent)
    assert "spawn_researcher" not in child


def test_child_registry_is_one_level_deeper():
    parent = parent_registry()
    assert parent.depth == 0
    assert build_child_registry(parent).depth == 1
    assert build_child_registry(build_child_registry(parent)).depth == 2


def test_child_registry_is_a_distinct_object():
    """Sharing the parent's registry would let a child mutate parent state."""
    parent = parent_registry()
    child = build_child_registry(parent)
    assert child is not parent

    @tool(description="late addition", tags={"read"})
    async def late() -> dict:
        return {}

    parent.register(late)
    assert "late" not in child


# --- depth enforcement -----------------------------------------------------

async def test_delegation_at_the_depth_cap_is_refused_with_a_reason():
    """The tool stays visible; the call explains why it cannot run.

    Hiding the tool would leave the model with no way to know delegation exists
    but is exhausted — it would simply never try, and never explain.
    """
    registry = parent_registry()
    registry.depth = 1                       # pretend we are already a child
    registry, record = attach_delegation(registry, llm=ScriptedLLM([]), max_depth=1)

    spawn = registry.get("spawn_researcher")
    result = await spawn.fn(question="what do they sell?")

    assert "error" in result
    assert result["reason"] == "max_depth_reached"
    assert record.refused == 1
    assert record.children == []


async def test_delegation_child_cannot_delegate_further_end_to_end():
    """maxDepth=1 means a grandchild is impossible, not merely discouraged."""
    # The parent delegates; the child is offered a registry with no spawn tool.
    child_llm = ScriptedLLM([final("The German market is dominated by wholesalers.")])
    record = DelegationRecord()
    registry, _ = attach_delegation(parent_registry(), llm=child_llm, record=record, max_depth=1)

    parent_llm = ScriptedLLM([
        call("spawn_researcher", {"question": "who dominates the German market?"}),
        final("Delegated and concluded from the child's answer."),
    ])
    loop = AgentLoop(parent_llm, registry, budget=AgentBudget(max_rounds=5))
    run = await loop.run("research the market")

    assert run.status == "succeeded"
    assert len(record.children) == 1
    assert record.children[0].ok is True

    # The child was offered no way to delegate further.
    offered = [entry["function"]["name"] for entry in child_llm.calls[0].tools]
    assert "spawn_researcher" not in offered
    assert "read_big" in offered


async def test_child_limit_is_enforced():
    registry = parent_registry()
    registry, record = attach_delegation(
        registry, llm=ScriptedLLM([final("answer one"), final("answer two")]),
        max_children=1,
    )

    spawn = registry.get("spawn_researcher")
    first = await spawn.fn(question="first question about the market")
    assert "answer" in first

    second = await spawn.fn(question="second question about the market")
    assert "error" in second
    assert second["reason"] == "max_children_reached"
    assert record.refused == 1


# --- context isolation -----------------------------------------------------

async def test_the_childs_working_stays_out_of_the_parents_context():
    """The whole point: the parent receives a conclusion, not a transcript."""
    big_result = await read_big(url="https://example.com")   # ~4400 chars of page text

    child_llm = ScriptedLLM([
        call("read_big", {"url": "https://example.com"}),
        final("They import embroidery thread and publish a purchasing address."),
    ])
    parent_llm = ScriptedLLM([
        call("spawn_researcher", {"question": "what does example.com do?"}),
        final("example.com imports embroidery thread. Delegation complete."),
    ])

    registry = parent_registry()
    registry, record = attach_delegation(registry, llm=child_llm, record=DelegationRecord())
    loop = AgentLoop(parent_llm, registry, budget=AgentBudget(max_rounds=5))
    run = await loop.run("find out what example.com does")

    assert run.status == "succeeded"
    assert len(record.children) == 1
    child = record.children[0]
    assert child.ok is True
    assert "embroidery thread" in child.answer

    # The parent's transcript must never contain the page text.
    parent_text = " ".join(
        (m.content or "") for m in run.messages
    )
    assert "PAGE TEXT" not in parent_text, (
        "the child's tool output leaked into the parent's context; delegation "
        "saved no context at all"
    )
    # The child read ~4400 characters; the parent carries a short conclusion.
    assert len(child.answer) < 500

    # But the parent does learn how the answer was established.
    observation = next(
        c.observation for s in run.steps for c in s.tool_calls if c.name == "spawn_researcher"
    )
    assert "how_it_was_established" in observation
    assert "read_big" in observation


async def test_the_child_actually_gets_its_own_transcript():
    child_llm = ScriptedLLM([
        call("read_big", {"url": "https://a.example"}),
        call("read_big", {"url": "https://b.example"}),
        final("Both sites import textile trims."),
    ])
    registry = parent_registry()
    registry, record = attach_delegation(registry, llm=child_llm, record=DelegationRecord())

    spawn = registry.get("spawn_researcher")
    result = await spawn.fn(question="do these two sites import trims?")

    assert "answer" in result
    child = record.children[0]
    assert child.run.tool_sequence == ["read_big", "read_big"]
    assert len(child.run.steps) == 3
    # The child's own trace holds the detail the parent never saw.
    assert child.run.messages


# --- failure isolation -----------------------------------------------------

async def test_a_child_that_stalls_returns_an_error_not_an_answer():
    """A child that did not conclude must not be reported as having concluded."""
    child_llm = ScriptedLLM([{"content": ""}, {"content": ""}, {"content": ""}])
    registry = parent_registry()
    registry, record = attach_delegation(registry, llm=child_llm, record=DelegationRecord())

    spawn = registry.get("spawn_researcher")
    result = await spawn.fn(question="what do they sell?")

    assert "error" in result
    assert "did not reach an answer" in result["error"]
    assert result["child_status"] == "stalled"
    assert record.children[0].ok is False


async def test_a_child_answer_without_investigation_is_not_accepted():
    """Same rule as the parent: prose with no tool use is not a finding."""
    child_llm = ScriptedLLM([
        {"content": "I believe they are a wholesaler."},   # short, no tools
        {"content": "Still just guessing here."},           # short again
        {"content": "Nothing further."},                    # short again -> stall
    ])
    registry = parent_registry()
    registry, record = attach_delegation(registry, llm=child_llm, record=DelegationRecord())
    result = await registry.get("spawn_researcher").fn(question="what do they do?")

    assert "error" in result
    assert record.children[0].ok is False


async def test_the_parent_survives_a_child_that_raises():
    """A child failure is a tool failure. It must not abort the parent."""
    class Exploding:
        async def chat(self, *args, **kwargs):
            raise RuntimeError("child provider down")

    registry = parent_registry()
    registry, record = attach_delegation(registry, llm=Exploding(), record=DelegationRecord())

    parent_llm = ScriptedLLM([
        call("spawn_researcher", {"question": "anything useful?"}),
        final("Delegation failed so I answered from the tools I have."),
    ])
    loop = AgentLoop(parent_llm, registry, budget=AgentBudget(max_rounds=5))
    run = await loop.run("research")

    assert run.status == "succeeded", "the parent must survive a child failure"
    observation = next(
        c.observation for s in run.steps for c in s.tool_calls if c.name == "spawn_researcher"
    )
    assert "fail" in observation.lower() or "error" in observation.lower()
    assert record.children[0].ok is False


async def test_the_parent_is_told_to_answer_itself_when_delegation_fails():
    registry = parent_registry()
    registry, _ = attach_delegation(
        registry,
        llm=ScriptedLLM([{"content": ""}, {"content": ""}, {"content": ""}]),
        record=DelegationRecord(),
    )
    result = await registry.get("spawn_researcher").fn(question="what do they do?")
    assert "yourself" in result["error"].lower()


# --- argument handling -----------------------------------------------------

async def test_empty_question_is_rejected():
    registry = parent_registry()
    registry, _ = attach_delegation(registry, llm=ScriptedLLM([]), record=DelegationRecord())
    result = await registry.get("spawn_researcher").fn(question="   ")
    assert "error" in result


async def test_focus_is_passed_along_with_the_question():
    child_llm = ScriptedLLM([call("list_customers", {}), final("Answered with focus.")])
    registry = parent_registry()
    registry, _ = attach_delegation(registry, llm=child_llm, record=DelegationRecord())

    result = await registry.get("spawn_researcher").fn(
        question="which customers need attention?", focus="the quoting stage"
    )
    assert "answer" in result
    # The focus reached the child as part of its prompt.
    child_prompt = child_llm.calls[0][-1].content
    assert "which customers need attention?" in child_prompt
    assert "the quoting stage" in child_prompt


async def test_attaching_twice_yields_two_independent_scopes():
    """Each attachment gets its own registry and its own record.

    The previous contract registered into the caller's registry and skipped on a
    duplicate name. That looked idempotent but was not: ``spawn_researcher``
    closes over the record for one run, so the second attachment silently reused
    the first run's tool. The second run then held a record nothing would ever
    write to, while the first run's record collected the second run's children.
    """
    base = parent_registry()

    scoped_a, record_a = attach_delegation(base, llm=ScriptedLLM([]))
    scoped_b, record_b = attach_delegation(base, llm=ScriptedLLM([]))

    assert record_a is not record_b
    assert scoped_a is not scoped_b
    assert scoped_a.names().count("spawn_researcher") == 1
    assert scoped_b.names().count("spawn_researcher") == 1
    # The caller's registry is left alone, so it can serve further runs.
    assert "spawn_researcher" not in base


async def test_two_runs_sharing_a_base_registry_do_not_share_delegations():
    """The bug this guards, reproduced directly.

    With the old mutating contract: run A attached, run B's attachment was
    skipped, and A's record ended up holding B's child.
    """
    base = parent_registry()
    record_a = DelegationRecord()
    record_b = DelegationRecord()

    scoped_a, _ = attach_delegation(
        base, llm=ScriptedLLM([call("read_big", {"url": "https://a.example"}),
                               final("Answer belonging to run A.")]),
        record=record_a,
    )
    scoped_b, _ = attach_delegation(
        base, llm=ScriptedLLM([call("read_big", {"url": "https://b.example"}),
                               final("Answer belonging to run B.")]),
        record=record_b,
    )

    result_b = await scoped_b.get("spawn_researcher").fn(question="question for B")

    assert "answer" in result_b
    assert len(record_b.children) == 1, "run B's record must hold B's child"
    assert len(record_a.children) == 0, "run A's record must not collect B's child"


async def test_child_budget_is_smaller_than_the_parent_default():
    """A child that runs as long as its parent defeats the delegation."""
    from app.agent.service import DEFAULT_BUDGET

    assert CHILD_BUDGET.max_rounds < DEFAULT_BUDGET.max_rounds
    assert CHILD_BUDGET.max_tool_calls < DEFAULT_BUDGET.max_tool_calls
    assert CHILD_BUDGET.max_seconds < DEFAULT_BUDGET.max_seconds


async def test_delegation_record_serialises():
    registry = parent_registry()
    registry, record = attach_delegation(
        registry, llm=ScriptedLLM([call("list_customers", {}), final("One customer is active.")]),
        record=DelegationRecord(),
    )
    await registry.get("spawn_researcher").fn(question="is anyone active?")

    payload = record.as_dict()
    assert payload["spawned"] == 1
    assert payload["children"][0]["ok"] is True
    assert payload["children"][0]["tool_sequence"] == ["list_customers"]
    assert payload["children"][0]["run_id"]


# --- the tool description tells the truth ----------------------------------

def test_the_tool_description_states_the_limitation():
    """The model should know the child cannot write before it relies on it."""
    registry, _ = attach_delegation(parent_registry(), llm=ScriptedLLM([]))
    description = registry.get("spawn_researcher").description.lower()
    assert "own context" in description
    assert "cannot send" in description or "cannot" in description
    assert "conclusion" in description


# --- the production entry point --------------------------------------------

async def test_execute_offers_delegation_by_default(monkeypatch, tmp_path):
    """A run that would drag every page into one context can push the reading out."""
    import app.agent.service as service

    monkeypatch.setenv("AGENT_SPILL_DIR", str(tmp_path))
    registry = parent_registry()
    child_llm = ScriptedLLM([
        call("read_big", {"url": "https://example.com"}),
        final("They import embroidery thread and list a purchasing address."),
    ])
    parent_llm = ScriptedLLM([
        call("spawn_researcher", {"question": "what does example.com do?"}),
        final("Concluded via a sub-agent."),
    ])

    result = await service.execute(
        "find out what example.com does",
        llm=parent_llm, registry=registry, persist=False, spill_dir=tmp_path,
    )

    assert result.delegations is not None
    assert result.delegations["spawned"] == 1
    assert result.delegations["children"][0]["ok"] is True
    assert result.as_dict()["delegations"]["spawned"] == 1

    # The caller's registry stays a plain base: the run's delegation scope lives
    # in a copy, so the same registry can serve the next run unchanged.
    assert "spawn_researcher" not in registry


async def test_execute_can_disable_delegation(monkeypatch, tmp_path):
    """A caller that wants a single flat run can say so."""
    import app.agent.service as service

    registry = parent_registry()
    result = await service.execute(
        "look at one thing",
        llm=ScriptedLLM([final("A direct answer without delegating.")]),
        registry=registry, persist=False, spill_dir=tmp_path,
        allow_delegation=False,
    )

    assert "spawn_researcher" not in registry
    assert result.delegations is None
    assert result.as_dict().get("delegations") is None
