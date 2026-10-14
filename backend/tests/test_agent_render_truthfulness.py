"""The generated trace page must report the truth about the run.

Two defects were found by looking at a rendered page rather than by reading code:

  * The stats header read "rounds 0, tool calls 0, tokens 0/0" while the diagram
    directly beneath it drew ten tool calls. The page was rendered from a stored
    trace, which carries only the steps — the counts were defaulting to zero.
  * The conclusion was emitted as escaped text, so the reader saw literal ``##``
    and ``**`` instead of a formatted answer.

Both are the same class of failure the rest of this project is about: a surface
that states something confidently and wrongly.
"""
from __future__ import annotations

import re

import pytest

from app.agent.render import (
    derive_stats,
    normalise_markdown,
    render_html,
    render_markdown,
)


# --- derive_stats ----------------------------------------------------------

def _step(round_no, *calls):
    return {
        "round": round_no,
        "thought": "",
        "tool_calls": [
            {"name": name, "arguments": {}, "ok": ok, "observation": "", "elapsed": 0.0,
             "call_id": f"c{round_no}{i}", "refused_reason": reason}
            for i, (name, ok, reason) in enumerate(calls)
        ],
    }


def test_stats_are_derived_from_the_trace_not_defaulted_to_zero():
    """The bug: a stored run has no counts, so they defaulted to 0."""
    steps = [
        _step(1, ("list_customers", True, "")),
        _step(2, ("get_customer_signals", True, "")),
        _step(3, ("get_customer_assessment", True, "")),
    ]
    stats = derive_stats(steps)

    assert stats["rounds"] == 3
    assert stats["tool_calls"] == 3
    assert stats["tool_sequence"] == [
        "list_customers", "get_customer_signals", "get_customer_assessment",
    ]


def test_refused_calls_are_counted_separately_and_kept_out_of_the_path():
    """A call that never ran did not shape the run."""
    steps = [
        _step(1, ("list_customers", True, ""), ("inspect_website", False, "tool_budget_exhausted")),
    ]
    stats = derive_stats(steps)

    assert stats["tool_sequence"] == ["list_customers"]
    assert stats["tool_calls"] == 1
    assert stats["refused_calls"] == 1


def test_explicit_meta_wins_over_derivation():
    """A live run already knows its numbers; do not recompute them."""
    steps = [_step(1, ("a", True, ""))]
    stats = derive_stats(steps, {"rounds": 9, "tool_calls": 42, "tool_sequence": ["x"]})
    assert stats["rounds"] == 9
    assert stats["tool_calls"] == 42
    assert stats["tool_sequence"] == ["x"]


def test_empty_trace_derives_zeroes_without_crashing():
    stats = derive_stats([])
    assert stats["rounds"] == 0
    assert stats["tool_calls"] == 0
    assert stats["tool_sequence"] == []


# --- the rendered page -----------------------------------------------------

TRACE = [
    _step(1, ("list_customers", True, "")),
    _step(2, ("get_customer_signals", True, ""), ("get_customer_signals", True, "")),
]


def test_page_reports_the_real_counts():
    page = render_html({
        "goal": "review the pipeline",
        "status": "succeeded",
        "stop_reason": "final answer",
        "steps": TRACE,
        "final_output": "Done.",
    })

    assert "<dd>2</dd>" in page, "rounds should be 2"
    assert "<dd>3</dd>" in page, "tool calls should be 3"
    assert "(none)" not in page.split("</dl>")[0], "the tool path must not read '(none)'"
    assert "list_customers" in page


def test_page_shows_refused_calls_as_a_reported_number():
    page = render_html({
        "goal": "g", "status": "truncated", "stop_reason": "tool-call budget exhausted",
        "steps": [_step(1, ("a", True, ""), ("b", False, "tool_budget_exhausted"))],
    })
    assert "<dt>refused</dt>" in page
    assert "<dd>1</dd>" in page


def test_page_renders_a_missing_conclusion_honestly():
    """A run with no answer must not look like a run with an empty answer."""
    page = render_html({
        "goal": "g", "status": "stalled",
        "stop_reason": "model produced no usable tool calls", "steps": [],
    })
    assert "No conclusion" in page
    assert "stalled" in page


def test_page_renders_the_conclusion_as_markdown():
    page = render_html({
        "goal": "g", "status": "succeeded", "stop_reason": "final answer",
        "steps": TRACE,
        "final_output": "## Answer\n\n**EuroTextil** is the one.\n\n| A | B |\n|---|---|\n| 1 | 2 |",
    })
    assert "<h3>Answer</h3>" in page
    assert "<strong>EuroTextil</strong>" in page
    assert "<table>" in page
    # The raw markers must be gone.
    assert "**EuroTextil**" not in page
    assert "|---|---|" not in page


def test_a_long_final_thought_is_abbreviated_outside_the_conclusion():
    """The conclusion is rendered once; the timeline and diagram abbreviate it.

    Asserted by length, not by counting a repeated phrase: a 400-character
    window contains a dozen repetitions of a short sentence, so a count says
    nothing about whether truncation happened.
    """
    phrase = "A long paragraph of reasoning."
    conclusion = "## Answer\n\n" + (phrase + " ") * 120
    step = {
        "round": 1, "thought": conclusion, "tool_calls": [
            {"name": "t", "arguments": {}, "ok": True, "observation": "", "elapsed": 0.0,
             "call_id": "c1", "refused_reason": ""},
        ],
    }
    page = render_html({
        "goal": "g", "status": "succeeded", "stop_reason": "final answer",
        "steps": [step], "final_output": conclusion,
    })

    start = page.find('class="conclusion"')
    end = page.find("</section>", start)
    assert start > 0 and end > start, "the conclusion section is missing"

    # The conclusion holds the answer in full...
    assert phrase in page[start:end]

    # ...and nothing outside it repeats the whole thing.
    rest = page[:start] + page[end:]
    assert phrase not in rest or rest.count(phrase) < 20, "the final thought was echoed in full"
    assert "full answer in the conclusion" in rest or "full answer is the conclusion" in rest

    # The timeline's copy is capped.
    timeline = page[page.find("<ol>"):page.find("</ol>")]
    thought = re.search(r'<div class="thought[^"]*">(.*?)</div>', timeline, re.DOTALL)
    assert thought is not None
    assert len(thought.group(1)) <= 450, (
        f"the timeline shows {len(thought.group(1))} characters of the conclusion"
    )


def test_a_short_final_thought_is_kept():
    """A brief closing line is useful context, not noise."""
    page = render_html({
        "goal": "g", "status": "succeeded", "stop_reason": "final answer",
        "steps": [{"round": 1, "thought": "Done.", "tool_calls": [
            {"name": "t", "arguments": {}, "ok": True, "observation": "", "elapsed": 0.0,
             "call_id": "c1", "refused_reason": ""}]}],
        "final_output": "Done.",
    })
    assert "<div class=\"thought\">Done.</div>" in page


def test_refused_calls_are_labelled_in_the_timeline():
    page = render_html({
        "goal": "g", "status": "truncated", "stop_reason": "budget", "steps": [
            {"round": 1, "thought": "", "tool_calls": [
                {"name": "a", "arguments": {}, "ok": False, "observation": "NOT EXECUTED: budget",
                 "elapsed": 0.0, "call_id": "c1", "refused_reason": "tool_budget_exhausted"}]}],
    })
    assert "refused: tool_budget_exhausted" in page


# --- zoom controls on the standalone page ----------------------------------
#
# Without these the diagram was squeezed into the page width and could not be
# enlarged — a ten-participant sequence diagram became unreadable, which defeats
# the point of showing it.

def test_the_diagram_has_zoom_controls():
    page = render_html({
        "goal": "g", "status": "succeeded", "stop_reason": "final answer",
        "steps": TRACE, "final_output": "Done.",
    })
    for control in ('id="zoom-in"', 'id="zoom-out"', 'id="zoom-reset"',
                    'id="zoom-full"', 'id="viewport"', 'id="canvas"'):
        assert control in page, f"missing zoom control: {control}"


def test_zoom_controls_have_no_duplicate_ids():
    """A duplicated id made the percentage label unreachable by getElementById."""
    page = render_html({
        "goal": "g", "status": "succeeded", "stop_reason": "final answer",
        "steps": TRACE, "final_output": "Done.",
    })
    for element_id in ("zoom-in", "zoom-out", "zoom-reset", "zoom-full", "viewport", "canvas"):
        assert page.count(f'id="{element_id}"') == 1, f"duplicate id: {element_id}"


def test_diagram_is_configured_not_to_squeeze():
    """useMaxWidth would let Mermaid shrink the SVG to its parent, undoing zoom."""
    page = render_html({
        "goal": "g", "status": "succeeded", "stop_reason": "final answer",
        "steps": TRACE, "final_output": "Done.",
    })
    assert "useMaxWidth: false" in page
    # And sanitisation stays strict: labels carry scraped page text.
    assert 'securityLevel: "strict"' in page


def test_standalone_page_has_no_app_shell_dependency():
    """It must open from disk, in a new tab, with no frontend build."""
    page = render_html({
        "goal": "g", "status": "succeeded", "stop_reason": "final answer",
        "steps": TRACE, "final_output": "Done.",
    })
    assert page.startswith("<!DOCTYPE html>")
    assert 'mermaid@11' in page
    assert "<script" in page


# --- render_markdown -------------------------------------------------------

def test_markdown_basics():
    out = render_markdown("# H1\n\ntext\n\n- a\n- b\n\n1. one\n\n---")
    assert "<h2>H1</h2>" in out          # an h1 in a conclusion is demoted
    assert "<p>text</p>" in out
    assert "<ul>" in out and out.count("<li>") == 3
    assert "<hr/>" in out


def test_markdown_tables_drop_the_separator_row():
    out = render_markdown("| A | B |\n|---|---|\n| 1 | 2 |")
    assert "<th>A</th>" in out and "<td>1</td>" in out
    assert "---" not in out


def test_markdown_inline_formatting():
    out = render_markdown("a **bold** and *em* and `code`")
    assert "<strong>bold</strong>" in out
    assert "<em>em</em>" in out
    assert "<code>code</code>" in out


def test_code_fence_is_a_block_not_a_paragraph():
    """<p><pre> is invalid HTML and browsers reparent it."""
    out = render_markdown("before\n\n```json\n{\"a\": 1}\n```\n\nafter")
    assert 'class="md-block"' in out
    assert "&quot;a&quot;" in out
    assert "<p><pre" not in out
    assert "<pre class=\"md-block\"><p>" not in out


def test_markdown_escapes_html_before_formatting():
    """Escaping first is what removes the need for a sanitiser."""
    out = render_markdown("<script>alert('x')</script>\n\n<img src=x onerror=alert(1)>")
    assert "<script>" not in out
    assert "<img" not in out
    assert "&lt;script&gt;" in out


def test_markdown_never_produces_an_href():
    """Links are deliberately not converted, so a javascript: URL is inert text."""
    for payload in ("[click](javascript:alert(1))", "[x](https://evil.example)"):
        out = render_markdown(payload)
        assert "href" not in out
        assert "<a " not in out


def test_markdown_handles_html_injected_inside_formatting():
    out = render_markdown("**<b>bold</b>**")
    assert "<b>" not in out
    assert "&lt;b&gt;" in out
    assert "<strong>" in out


def test_markdown_empty_and_plain():
    assert render_markdown("") == ""
    assert render_markdown("hi") == "<p>hi</p>"


def test_markdown_unclosed_fence_does_not_swallow_the_page():
    out = render_markdown("intro\n\n```\nunclosed content")
    assert "intro" in out
    assert "unclosed content" in out


@pytest.mark.parametrize("n", [1, 2])
def test_derive_stats_is_stable(n):
    steps = [_step(i, ("t", True, "")) for i in range(n)]
    assert derive_stats(steps)["rounds"] == n
    assert derive_stats(steps)["rounds"] == n


# --- normalise_markdown ----------------------------------------------------
#
# Models frequently return an entire answer on one line, with block structure
# implied by markers rather than by newlines. Without this the answer rendered as
# one dense paragraph and the markers meant as formatting showed up literally.

ONELINE = (
    "## Answer: EuroTextil **Reasoning:** it is the only one in *quoting*. "
    "**Numbers used:** - EuroTextil: 72 - ABC: 85 **Caveat:** the data contradicts itself."
)


def test_one_line_answer_gains_block_structure():
    out = render_markdown(ONELINE)
    assert "<h3>Answer:" in out
    assert out.count("<strong>") >= 3
    assert "<ul>" in out
    assert "<em>quoting</em>" in out


def test_one_line_answer_has_no_stray_asterisks():
    """The visible symptom: literal `**` left on the page."""
    out = render_markdown(ONELINE)
    without_tags = out.replace("<strong>", "").replace("</strong>", "")
    assert "*" not in without_tags


def test_bold_runs_are_balanced():
    out = render_markdown(ONELINE)
    assert out.count("<strong>") == out.count("</strong>")


def test_a_parenthesised_number_is_not_a_list_marker():
    """'(id 2)' must not split the heading.

    An earlier version broke at any digits followed by ')', which turned the "2"
    in "(id 2) most needs attention" into an ordered-list item and cut the
    heading in half.
    """
    src = "## Answer: EuroTextil GmbH (id 2) most needs attention today"
    out = render_markdown(src)
    assert "(id 2) most needs attention today" in out
    assert "<ol>" not in out


def test_a_decimal_is_not_a_sentence_boundary():
    """'72.0 - ABC' must keep its hyphen.

    Scores, prices and percentages are full of decimal points in this domain,
    and treating one as a sentence ending split a list item in the wrong place.
    """
    assert normalise_markdown("score 72.0 - ABC Fashion") == "score 72.0 - ABC Fashion"


def test_a_real_sentence_boundary_does_split():
    assert normalise_markdown("Done. - a") == "Done.\n- a"


def test_well_formed_input_is_left_unchanged():
    for src in (
        "## Head\n\n- a\n- b\n\ntext **bold**\n",
        "## Head\n\n| A | B |\n|---|---|\n| 1 | 2 |\n",
        "## H\n\n1. first\n2. second\n",
        "First paragraph.\n\nSecond paragraph.\n",
    ):
        assert normalise_markdown(src) == src, f"normalisation changed well-formed input: {src!r}"


def test_well_formed_table_is_still_rendered_as_a_table():
    """A pipe rule must not break the case that already worked.

    An earlier attempt at normalising inline tables broke well-formed ones —
    a regression worse than the thing it was fixing. Pipes are not normalised
    at all.
    """
    out = render_markdown("## Head\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
    assert out.count("<table>") == 1
    assert out.count("<th>") == 2
    assert out.count("<td>") == 2


def test_inline_table_is_left_as_readable_text():
    """A stated limitation, not an accident.

    Tables emitted inline on one line are genuinely ambiguous, and two attempts
    at normalising them caused worse problems than leaving them. They render as
    text; a table on its own lines renders as a table.
    """
    out = render_markdown("## T | A | B | |---|---| | 1 | 2 |")
    assert "<h3>T" in out
    assert "<table>" not in out
    assert "| 1 | 2 |" in out          # still readable


@pytest.mark.parametrize(
    "probe,why",
    [
        ("See https://x.example/a#section", "a URL fragment must not become a heading"),
        ("issue #1 and #2", "issue numbers must not become headings"),
        ("values -1 and -2", "negative numbers must not become bullets"),
        ("1.5 and 2.5", "decimals must not become list items"),
        ("a*b*c", "intra-word asterisks must not become emphasis"),
        ("a | b in prose", "a pipe in prose must not start a table row"),
        ("text **bold** here", "already-formatted text must survive untouched"),
        ("The market grew 3% (id 2) last year", "a mid-sentence parenthetical number"),
        ("He said hello. Then he left.", "ordinary prose"),
        ("Trade fair, personal mailbox, or phone", "a comma-separated list"),
        ("score 72.0 - ABC Fashion", "a hyphen after a decimal"),
    ],
)
def test_normalisation_never_damages_safe_constructs(probe, why):
    assert normalise_markdown(probe) == probe, why


def test_normalisation_preserves_fenced_blocks():
    src = "intro\n\n```python\nx = 1  # - not a bullet\n```\n"
    out = normalise_markdown(src)
    assert "x = 1  # - not a bullet" in out


def test_normalisation_is_idempotent():
    once = normalise_markdown(ONELINE)
    assert normalise_markdown(once) == once


def test_normalisation_handles_empty_input():
    assert normalise_markdown("") == ""


# --- the route's wiring ----------------------------------------------------
#
# The tests above pass synthetic dicts to render_html, so they cannot catch a
# route that forgets to pass a field. This one goes through the HTTP endpoint,
# which is where the conclusion was silently rendered empty.

async def test_trace_html_route_includes_the_conclusion(client, db):
    from app.agent.loop import AgentBudget, run_agent
    from app.agent.repository import save_run
    from app.agent.service import build_registry
    from tests.scripted_llm import ScriptedLLM, call, final

    run = await run_agent(
        "check the pipeline",
        ScriptedLLM([
            call("list_customers", {}),
            final("## Verdict\n\n**One** customer needs attention.\n\n| A | B |\n|---|---|\n| 1 | 2 |"),
        ]),
        build_registry(),
        budget=AgentBudget(max_rounds=4),
    )
    await save_run(db, run)

    page = client.get(f"/api/v1/agent/runs/{run.run_id}/trace.html").text

    # The route must pass final_output through, or this section renders empty.
    assert 'class="conclusion"' in page, "the route did not pass final_output to render_html"
    assert "<h3>Verdict</h3>" in page, "the conclusion was not rendered as markdown"
    assert "<strong>One</strong>" in page
    assert "<table>" in page
    # And the header must not contradict the trace.
    assert "(none)" not in page.split("</dl>")[0]
    assert "<dd>1</dd>" in page       # one tool call


async def test_trace_html_route_flags_a_run_with_no_conclusion(client, db):
    from app.agent.loop import AgentBudget, run_agent
    from app.agent.repository import save_run
    from app.agent.service import build_registry
    from tests.scripted_llm import ScriptedLLM

    run = await run_agent(
        "g", ScriptedLLM([{"content": ""}, {"content": ""}, {"content": ""}]),
        build_registry(), budget=AgentBudget(max_rounds=5, max_idle_rounds=3),
    )
    await save_run(db, run)

    page = client.get(f"/api/v1/agent/runs/{run.run_id}/trace.html").text
    assert "No conclusion" in page
    assert run.status == "stalled"
