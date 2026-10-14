"""Trace rendering: Mermaid output must be valid, stable, and injection-safe."""
from __future__ import annotations

import pytest

from app.agent import AgentBudget, AgentLoop, ToolRegistry, tool
from app.agent.render import (
    mermaid_id,
    mermaid_label,
    render_html,
    render_mermaid_flowchart,
    render_mermaid_sequence,
    render_mermaid_sequence_comparison,
)
from tests.fakes import FakeLLM, final, tool_reply


@tool(description="Search prospects")
async def search_prospects(keyword: str) -> dict:
    return {"found": 1, "keyword": keyword}


@tool(description="Inspect a site")
async def inspect_website(url: str) -> dict:
    return {"url": url, "emails": ["buyer@example.com"]}


@tool(description="Fails")
async def explode(x: str = "") -> dict:
    raise RuntimeError("upstream 500")


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in (search_prospects, inspect_website, explode):
        reg.register(t)
    return reg


async def _run(script, registry, goal="find buyers"):
    return await AgentLoop(FakeLLM(script), registry, budget=AgentBudget()).run(goal)


# --- sanitisers ------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("plain_name", "plain_name"),
        ("with-dash", "with_dash"),
        ("with.dot", "with_dot"),
        ("9leading_digit", "t_9leading_digit"),
        ("", "unknown"),
        ("!!!", "___"),
    ],
)
def test_mermaid_id_is_alnum_underscore(raw, expected):
    assert mermaid_id(raw) == expected


def test_mermaid_label_strips_newlines_and_quotes():
    label = mermaid_label('multi\nline "quoted" text')
    assert "\n" not in label
    assert '"' not in label
    assert "'quoted'" in label


def test_mermaid_label_truncates():
    label = mermaid_label("z" * 500, limit=40)
    assert len(label) <= 40
    assert label.endswith("…")


def test_mermaid_label_handles_none():
    assert mermaid_label(None) == ""


# --- sequence diagram ------------------------------------------------------

async def test_sequence_contains_every_participant_and_call(registry):
    run = await _run(
        [
            tool_reply("search_prospects", {"keyword": "yarn"}),
            tool_reply("inspect_website", {"url": "https://a.de"}),
            final("Done."),
        ],
        registry,
    )
    diagram = render_mermaid_sequence(run)

    assert diagram.startswith("---\ntitle:")
    assert "sequenceDiagram" in diagram
    assert "participant search_prospects" in diagram
    assert "participant inspect_website" in diagram
    assert "search_prospects(keyword=yarn)" in diagram
    assert "inspect_website(url=https://a.de)" in diagram
    assert "succeeded" in diagram


async def test_sequence_marks_failed_calls(registry):
    run = await _run([tool_reply("explode", {}), final("gave up")], registry)
    diagram = render_mermaid_sequence(run)
    assert "FAILED" in diagram
    assert "RuntimeError" in diagram


async def test_sequence_notes_the_final_status(registry):
    """A run that never finishes is reported as truncated, with the reason."""
    run = await AgentLoop(
        FakeLLM([tool_reply("explode", {}) for _ in range(4)]),
        registry,
        budget=AgentBudget(max_rounds=4),
    ).run("keep failing")

    assert run.status == "truncated"
    diagram = render_mermaid_sequence(run)
    assert "truncated" in diagram
    assert "round budget exhausted" in diagram


async def test_sequence_handles_a_run_with_no_steps(registry):
    """A run cut off by the time budget before round 1 has an empty trace."""
    run = await AgentLoop(
        FakeLLM([tool_reply("search_prospects", {"keyword": "yarn"})]),
        registry,
        budget=AgentBudget(max_seconds=0.0),
    ).run("find buyers")

    assert run.steps == []
    diagram = render_mermaid_sequence(run)
    assert "stopped before any round ran" in diagram
    assert "truncated" in diagram


async def test_sequence_handles_a_round_with_no_tool_calls(registry):
    run = await _run([final("nothing needed, answering directly")], registry)
    diagram = render_mermaid_sequence(run)
    assert "round 1: no tool call" in diagram


def test_sequence_accepts_plain_dicts():
    """The renderer must work on decoded DB traces, not just live objects."""
    as_dict = {
        "goal": "g",
        "status": "succeeded",
        "stop_reason": "final answer",
        "tool_sequence": ["a"],
        "steps": [
            {
                "round": 1,
                "thought": "thinking",
                "tool_calls": [
                    {"name": "a", "arguments": {"k": "v"}, "ok": True, "observation": "obs", "elapsed": 0.1}
                ],
            }
        ],
    }
    diagram = render_mermaid_sequence(as_dict)
    assert "participant a" in diagram
    assert "a(k=v)" in diagram


# --- flowchart -------------------------------------------------------------

async def test_flowchart_shows_the_path(registry):
    run = await _run(
        [tool_reply("search_prospects", {"keyword": "yarn"}), final("done")],
        registry,
    )
    chart = render_mermaid_flowchart(run)
    assert chart.startswith("---\ntitle:")
    assert "flowchart TD" in chart
    assert "search_prospects(keyword=yarn)" in chart
    assert "succeeded" in chart


async def test_flowchart_marks_failed_nodes_with_a_different_shape(registry):
    run = await _run([tool_reply("explode", {}), final("done")], registry)
    chart = render_mermaid_flowchart(run)
    assert "[/" in chart     # parallelogram = failure


# --- injection safety ------------------------------------------------------

async def test_malicious_observation_cannot_break_the_diagram(registry):
    """Observations contain scraped web text — treat them as untrusted.

    The threat is newline injection: if a label's newlines were preserved, a
    hostile web page could end our statement and append its own Mermaid
    directives. The payload text legitimately appears *inside* a label (that
    is fine and desirable — you want to see what the tool returned); what
    must never happen is the payload occupying its own line.
    """

    @tool(description="returns hostile content")
    async def hostile() -> dict:
        return {"text": 'evil\n    participant INJECTED\n    U->>INJECTED: pwned\n"quoted"'}

    reg = ToolRegistry()
    reg.register(hostile)
    run = await AgentLoop(FakeLLM([tool_reply("hostile", {}), final("done")]), reg).run('goal "with quotes"')

    # The payload is pre-escaped by the renderer. Find what it became.
    hostile_escaped = [m for m in ("participant INJECTED",) if m in render_mermaid_sequence(run)]
    assert hostile_escaped, "sanity: the payload should still be visible, escaped, inside a label"

    valid_prefixes = (
        "participant ",
        "autonumber",
        "U->>",
        "A->>",
        "A-->>",
        "Note over",
    )
    for diagram in (render_mermaid_sequence(run), render_mermaid_flowchart(run)):
        body = diagram.split("---", 2)[-1]
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # A plain participant line for a *tool* we never registered would
            # mean injection succeeded.
            if stripped.startswith("participant "):
                assert stripped.startswith(("participant U ", "participant A ")) or (
                    " INJECTED" not in stripped
                ), f"injected participant declaration: {stripped!r}"
            # The hostile payload must never begin a line — that is the only
            # position from which Mermaid would treat it as a statement.
            assert not stripped.startswith("U->>INJECTED"), f"injected message: {stripped!r}"
        # The doubled-quote escape means the payload cannot close our label.
        assert '"quoted"' not in body


async def test_html_is_escaped(registry):
    @tool(description="returns script")
    async def scripty() -> dict:
        return {"x": "<script>alert(1)</script>"}

    reg = ToolRegistry()
    reg.register(scripty)
    run = await AgentLoop(FakeLLM([tool_reply("scripty", {}), final("done")]), reg).run("<img src=x onerror=1>")
    page = render_html(run)

    assert "<img src=x onerror=1>" not in page
    assert "&lt;img src=x onerror=1&gt;" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


async def test_html_contains_the_timeline_and_stats(registry):
    run = await _run(
        [tool_reply("search_prospects", {"keyword": "yarn"}), final("did it")],
        registry,
    )
    page = render_html(run)
    assert page.startswith("<!DOCTYPE html>")
    assert "Round 1" in page
    assert "Round 2" in page
    assert "search_prospects" in page
    assert "tool path:" in page
    assert 'class="mermaid"' in page


# --- comparison ------------------------------------------------------------

async def test_comparison_puts_both_runs_in_one_diagram(registry):
    run_new = await _run(
        [tool_reply("search_prospects", {"keyword": "yarn"}), final("found prospects")],
        registry,
        goal="find new buyers",
    )
    run_existing = await _run(
        [tool_reply("inspect_website", {"url": "https://known.de"}), final("reviewed pipeline")],
        registry,
        goal="review existing customers",
    )

    diagram = render_mermaid_sequence_comparison(
        [("new-lead hunt", run_new), ("pipeline review", run_existing)]
    )
    assert "new-lead hunt" in diagram
    assert "pipeline review" in diagram
    assert "search_prospects(keyword=yarn)" in diagram
    assert "inspect_website(url=https://known.de)" in diagram
    # Both tools are declared once, shared across the runs.
    assert diagram.count("participant search_prospects") == 1


# --- determinism -----------------------------------------------------------

async def test_rendering_is_deterministic(registry):
    run = await _run([tool_reply("search_prospects", {"keyword": "yarn"}), final("done")], registry)
    assert render_mermaid_sequence(run) == render_mermaid_sequence(run)
    assert render_mermaid_flowchart(run) == render_mermaid_flowchart(run)
