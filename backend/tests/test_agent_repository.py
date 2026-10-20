"""Agent run persistence: the audit trail must round-trip faithfully."""
from __future__ import annotations

import json

import pytest

from app.agent import AgentBudget, AgentLoop, ToolRegistry, tool  # noqa: F401
from app.agent.repository import get_run, list_runs, save_run, trace_of
from app.database import init_db
from tests.fakes import FakeLLM, final, tool_reply


@tool(description="Look up customers")
async def list_customers(stage: str = "all") -> dict:
    return {"stage": stage, "customers": [{"id": 1, "company_name": "ACME"}]}


@tool(description="Fails on purpose")
async def broken(reason: str = "x") -> dict:
    raise RuntimeError(reason)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(list_customers)
    reg.register(broken)
    return reg


@pytest.fixture
async def schema():
    """Ensure tables exist for tests that do not boot the full app."""
    await init_db()
    yield


async def _make_run(script, registry, goal="review the pipeline"):
    """Drive a real run through ``run_agent``.

    Uses the public wrapper rather than ``AgentLoop.run`` so LLM failures are
    converted into ``status="error"`` runs, exactly as production does.
    """
    from app.agent import run_agent

    return await run_agent(goal, FakeLLM(script), registry, budget=AgentBudget())


async def test_save_and_read_back(db, schema, registry):
    run = await _make_run([tool_reply("list_customers", {"stage": "new"}), final("done")], registry)
    row = await save_run(db, run)

    assert row.id is not None
    assert row.run_id == run.run_id
    assert row.status == "succeeded"
    assert row.rounds == 2
    assert row.tool_calls == 1
    assert row.final_output
    assert row.finished_at is not None

    fetched = await get_run(db, run.run_id)
    assert fetched is not None
    assert fetched.run_id == run.run_id


async def test_trace_round_trips(db, schema, registry):
    run = await _make_run(
        [tool_reply("list_customers", {"stage": "new"}, content="thinking"), final("done")],
        registry,
    )
    await save_run(db, run)

    row = await get_run(db, run.run_id)
    trace = trace_of(row)

    assert len(trace) == 2
    first = trace[0]
    assert first["round"] == 1
    assert first["thought"] == "thinking"
    call = first["tool_calls"][0]
    assert call["name"] == "list_customers"
    assert call["arguments"] == {"stage": "new"}
    assert call["ok"] is True
    assert "ACME" in call["observation"]

    # The decoded trace is directly renderable.
    from app.agent.render import render_mermaid_sequence

    diagram = render_mermaid_sequence({"goal": run.goal, "status": row.status,
                                       "stop_reason": row.stop_reason, "steps": trace})
    assert "list_customers(stage=new)" in diagram


async def test_saving_the_same_run_twice_updates_rather_than_duplicates(db, schema, registry):
    run = await _make_run([tool_reply("list_customers", {}), final("done")], registry)
    first = await save_run(db, run)

    run.final_output = "revised conclusion"
    run.status = "succeeded"
    second = await save_run(db, run)

    assert second.id == first.id
    assert second.final_output == "revised conclusion"

    all_rows = await list_runs(db, limit=100)
    assert len([r for r in all_rows if r.run_id == run.run_id]) == 1


async def test_failed_calls_are_recorded_truthfully(db, schema, registry):
    run = await _make_run([tool_reply("broken", {"reason": "upstream 500"}), final("done")], registry)
    await save_run(db, run)
    row = await get_run(db, run.run_id)
    call = trace_of(row)[0]["tool_calls"][0]
    assert call["ok"] is False
    assert "upstream 500" in call["observation"]


async def test_error_runs_are_persisted(db, schema, registry):
    run = await _make_run([RuntimeError("model exploded")], registry)
    assert run.status == "error"
    row = await save_run(db, run)
    assert row.status == "error"
    assert "model exploded" in row.error


@pytest.mark.parametrize("status", ["succeeded", "truncated", "stalled", "error"])
async def test_every_terminal_status_persists(db, schema, registry, status):
    """Each way a run can end must be storable and retrievable."""
    from app.agent.loop import AgentRun

    run = AgentRun(goal="g", status=status, stop_reason=f"reason-{status}")
    row = await save_run(db, run)
    assert row.status == status
    assert row.stop_reason == f"reason-{status}"


async def test_list_runs_is_newest_first_and_limitable(db, schema, registry):
    for index in range(3):
        run = await _make_run([final(f"answer number {index}")], registry, goal=f"goal {index}")
        await save_run(db, run)

    rows = await list_runs(db, limit=2)
    assert len(rows) == 2
    assert rows[0].id > rows[1].id


async def test_list_runs_can_filter_by_status(db, schema, registry):
    await save_run(db, await _make_run([final("ok")], registry, goal="good"))
    await save_run(db, await _make_run([RuntimeError("boom")], registry, goal="bad"))

    errors = await list_runs(db, status="error")
    assert errors
    assert all(r.status == "error" for r in errors)


async def test_get_run_returns_none_for_unknown_id(db, schema):
    assert await get_run(db, "no-such-run") is None


async def test_corrupt_trace_decodes_to_empty_instead_of_raising(db, schema, registry):
    run = await _make_run([tool_reply("list_customers", {}), final("done")], registry)
    row = await save_run(db, run)

    row.trace = "{not valid json"
    await db.commit()
    await db.refresh(row)

    assert trace_of(row) == []


async def test_trace_of_null_is_empty(db, schema):
    from app.models.agent_run import AgentRun as Row

    assert trace_of(Row(run_id="x", goal="g", trace=None)) == []


async def test_stored_trace_is_valid_json_text(db, schema, registry):
    run = await _make_run([tool_reply("list_customers", {"stage": "new"}), final("done")], registry)
    row = await save_run(db, run)
    json.loads(row.trace)      # must not raise


async def test_a_run_records_which_prompt_produced_it(db, schema, registry):
    """The row outlives the commit; its prompt fingerprint must not.

    Without this, "did quality drop because we changed the prompt, the model, or
    the customers?" has no answer in the data.
    """
    from app.agent import DEFAULT_SYSTEM_PROMPT
    from app.agent.prompts import fingerprint

    run = await _make_run([final("done")], registry)
    assert run.prompt_version == fingerprint(DEFAULT_SYSTEM_PROMPT)

    row = await save_run(db, run)
    assert row.prompt_version == run.prompt_version

    fetched = await get_run(db, run.run_id)
    assert fetched is not None
    assert fetched.prompt_version == run.prompt_version


async def test_a_different_prompt_gets_a_different_identity(db, schema, registry):
    """It fingerprints the prompt that was used, not a constant in the module."""
    from app.agent import DEFAULT_SYSTEM_PROMPT, run_agent

    plain = await run_agent("x", FakeLLM([final("a")]), registry)
    terse = await run_agent(
        "x", FakeLLM([final("b")]), registry,
        system_prompt=DEFAULT_SYSTEM_PROMPT + "\n\nBe brief.",
    )

    assert plain.prompt_version != terse.prompt_version


async def test_progress_writes_do_not_erase_the_prompt_identity(db, schema, registry):
    """``save_run`` is called repeatedly while a run is in progress.

    The prompt does not change between those writes. A later save that omitted
    the field must not null it out.
    """
    from dataclasses import replace

    run = await _make_run([final("done")], registry)
    await save_run(db, run)
    identity = run.prompt_version

    # A later save of the same run, as the progress callback would do.
    later = replace(run)
    later.prompt_version = ""
    later.status = "running"
    row = await save_run(db, later)

    assert row.prompt_version == identity
