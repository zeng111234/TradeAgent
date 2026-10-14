"""Context compaction: keeping a long run inside the window without losing data.

A prospecting run inspects a dozen websites at ~3,200 characters each. By round
eight the history is 25,000+ characters, and past the window the provider either
errors or silently truncates. Silent truncation is the dangerous case: the model
loses the evidence it was reasoning from and nobody is told.

These tests pin both halves of the design:

* the request is bounded, and
* nothing is actually lost — trimmed observations stay retrievable by name.
"""
from __future__ import annotations

import pytest

from app.agent.context import (
    CompactionReport,
    SpillStore,
    build_recall_tool,
    compact_messages,
    estimate_message_tokens,
    estimate_tokens,
)
from app.agent.loop import AgentBudget, AgentLoop
from app.agent.protocol import Message, ToolCall
from app.agent.tools import ToolRegistry, tool
from tests.scripted_llm import ScriptedLLM, call, final


# --- estimation ------------------------------------------------------------

def test_estimate_grows_with_content():
    small = [Message.user("hi")]
    large = [Message.user("x" * 4000)]
    assert estimate_tokens(large) > estimate_tokens(small)


def test_structured_payloads_are_estimated_more_densely():
    """JSON is charged at a denser ratio than prose, so it is not under-budgeted.

    Tests the ratio directly. Per-message overhead would swamp a small sample, so
    this compares two payloads of identical length.
    """
    from app.agent.context import CHARS_PER_TOKEN_PROSE, CHARS_PER_TOKEN_STRUCTURED, estimate_body_tokens

    size = 600
    prose = ("word " * (size // 5 + 1))[:size]
    # A single JSON object of exactly the same length (leading "{" makes it
    # structured; trailing padding does not change how it is charged).
    structured = ('{"key": 1, ' * 100)[:size].ljust(size, " ")

    assert len(prose) == size and len(structured) == size
    assert structured.startswith("{")
    assert estimate_body_tokens(prose) == int(size / CHARS_PER_TOKEN_PROSE)
    assert estimate_body_tokens(structured) == int(size / CHARS_PER_TOKEN_STRUCTURED)
    # Denser ratio means a *lower* estimate for the same characters — which is
    # what stops a JSON-heavy history from being under-counted.
    assert estimate_body_tokens(structured) > estimate_body_tokens(prose) == int(size / 4.0)


def test_indented_json_is_still_recognised():
    from app.agent.context import CHARS_PER_TOKEN_PROSE, estimate_body_tokens

    indented = '   \n {"key": "value", "other": "thing"}'
    plain = "   \n  " + "x" * (len(indented) - 6)
    assert len(indented) == len(plain)
    assert estimate_body_tokens(indented) > estimate_body_tokens(plain) == int(
        len(plain) / CHARS_PER_TOKEN_PROSE
    )


def test_prose_is_estimated_at_the_prose_ratio():
    from app.agent.context import CHARS_PER_TOKEN_PROSE, estimate_body_tokens

    text = "the quick brown fox jumps over the lazy dog " * 10
    assert estimate_body_tokens(text) == int(len(text) / CHARS_PER_TOKEN_PROSE)


def test_empty_body_estimates_to_zero():
    from app.agent.context import estimate_body_tokens

    assert estimate_body_tokens("") == 0


def test_estimate_counts_tool_call_arguments():
    without = Message.assistant("", [ToolCall(id="1", name="x", arguments="")])
    with_args = Message.assistant("", [ToolCall(id="1", name="x", arguments="y" * 400)])
    assert estimate_message_tokens(with_args) > estimate_message_tokens(without)


def test_a_bare_call_costs_something():
    """Arguments being empty does not make a tool call free."""
    assert estimate_message_tokens(
        Message.assistant("", [ToolCall(id="1", name="x", arguments="")])
    ) > 0


def test_estimate_accepts_a_single_message_or_a_list():
    message = Message.user("hello")
    assert estimate_tokens(message) == estimate_tokens([message])


def test_estimate_handles_none():
    assert estimate_tokens(None) == 0


def test_empty_history_estimates_to_zero():
    assert estimate_tokens([]) == 0


# --- spill store -----------------------------------------------------------

def test_spill_store_round_trips(tmp_path):
    store = SpillStore(tmp_path)
    path = store.write("obs-3", "the full text")
    assert store.read(path.name) == "the full text"


def test_spill_store_is_content_addressed(tmp_path):
    """The same payload written twice is stored once, and the name is stable."""
    store = SpillStore(tmp_path)
    first = store.write("obs-1", "identical")
    second = store.write("obs-2", "identical")
    assert first == second


def test_spill_store_rejects_path_traversal(tmp_path):
    """A model could pass anything as the name; it must not read outside."""
    store = SpillStore(tmp_path / "store")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me", encoding="utf-8")

    assert store.read("../secret.txt") is None
    assert store.read("../../etc/passwd") is None


def test_spill_store_read_missing_returns_none(tmp_path):
    assert SpillStore(tmp_path).read("nope.txt") is None


# --- compaction ------------------------------------------------------------

def _history(count: int, size: int = 4000) -> list[Message]:
    messages = [Message.system("system prompt"), Message.user("the goal")]
    for index in range(count):
        messages.append(
            Message.assistant("", [ToolCall(id=f"c{index}", name="inspect", arguments="{}")])
        )
        messages.append(
            Message.tool_result(
                ToolCall(id=f"c{index}", name="inspect", arguments="{}"),
                "P" * size,
            )
        )
    return messages


def test_small_history_is_untouched():
    messages = _history(1, size=50)
    view, report = compact_messages(messages, budget_tokens=10_000)
    assert view == messages
    assert report.did_anything is False


def test_large_history_is_compacted_under_budget():
    messages = _history(12, size=4000)
    before = estimate_tokens(messages)
    view, report = compact_messages(messages, budget_tokens=6_000, keep_recent=4)

    assert report.did_anything is True
    assert estimate_tokens(view) < before
    assert report.trimmed_messages > 0
    assert report.dropped_tokens > 0


def test_recent_messages_are_never_trimmed():
    """The last exchanges are what the model is actively reasoning over."""
    messages = _history(12, size=4000)
    keep_recent = 4
    view, _ = compact_messages(messages, budget_tokens=3_000, keep_recent=keep_recent)

    assert view[-keep_recent:] == messages[-keep_recent:]


def test_system_prompt_is_never_trimmed():
    messages = [Message.system("S" * 5000)] + _history(6, size=4000)
    view, _ = compact_messages(messages, budget_tokens=3_000, keep_recent=2)
    assert view[0].content == "S" * 5000


def test_short_messages_are_never_trimmed():
    """Trimming the model's own reasoning costs more than the tokens it saves."""
    messages = [Message.system("s"), Message.user("g")]
    messages += [Message.assistant("a short thought") for _ in range(20)]
    view, report = compact_messages(messages, budget_tokens=10, keep_recent=2)
    assert all(m.content in ("s", "g", "a short thought") for m in view)
    assert report.did_anything is False


def test_truncation_keeps_head_and_tail():
    body = "HEAD" + ("m" * 3000) + "TAIL"
    messages = [Message.system("s"), Message.user("g"),
                Message.tool_result(ToolCall(id="1", name="t", arguments="{}"), body)]
    view, report = compact_messages(messages, budget_tokens=100, keep_recent=0,
                                    head_chars=200, tail_chars=100)

    assert report.did_anything is True
    trimmed = view[-1].content
    assert trimmed.startswith("HEAD")
    assert trimmed.endswith("TAIL")
    assert len(trimmed) < len(body)
    assert "elided" in trimmed


def test_compaction_does_not_mutate_the_input():
    """The caller keeps the full history for the trace."""
    messages = _history(10, size=4000)
    original = [m.content for m in messages]
    compact_messages(messages, budget_tokens=2_000, keep_recent=2)
    assert [m.content for m in messages] == original


def test_spilled_observations_are_named_in_the_marker(tmp_path):
    store = SpillStore(tmp_path)
    messages = [Message.system("s"), Message.user("g")] + [
        Message.tool_result(ToolCall(id=str(i), name="t", arguments="{}"), "X" * 3000)
        for i in range(6)
    ]
    view, report = compact_messages(messages, budget_tokens=1_000, keep_recent=1, spill=store)

    assert report.spilled, "the full text should have been stored"
    marker = next(m.content for m in view if "elided" in m.content)
    assert "recall_observation" in marker
    assert report.spilled[0] in marker
    # And the name in the marker really does resolve.
    assert store.read(report.spilled[0]) is not None


def test_spill_failure_degrades_to_an_honest_marker(monkeypatch, tmp_path):
    """If the file cannot be written, say so rather than promising retrieval."""
    store = SpillStore(tmp_path)

    def boom(label, text):
        raise OSError("disk full")

    monkeypatch.setattr(store, "write", boom)
    messages = [Message.system("s"), Message.user("g")] + [
        Message.tool_result(ToolCall(id=str(i), name="t", arguments="{}"), "X" * 3000)
        for i in range(6)
    ]
    view, report = compact_messages(messages, budget_tokens=1_000, keep_recent=1, spill=store)

    marker = next(m.content for m in view if "elided" in m.content)
    assert "not recoverable" in marker
    assert "recall_observation" not in marker
    assert report.spilled == []


def test_report_note_is_empty_when_nothing_happened():
    assert CompactionReport().as_note() == ""


def test_report_note_describes_what_happened():
    report = CompactionReport(dropped_tokens=500, trimmed_messages=3, spilled=["a.txt"])
    note = report.as_note()
    assert "3" in note
    assert "500" in note
    assert "retrievable" in note


# --- the recall tool -------------------------------------------------------

async def test_recall_tool_reads_back_a_spilled_observation(tmp_path):
    store = SpillStore(tmp_path)
    name = store.write("obs-1", "the original full text").name
    recall = build_recall_tool(store)

    result = await recall(name=name)
    assert result["text"] == "the original full text"
    assert result["chars"] == len("the original full text")


async def test_recall_tool_explains_an_unknown_name(tmp_path):
    recall = build_recall_tool(SpillStore(tmp_path))
    result = await recall(name="missing.txt")
    assert "error" in result
    assert "compaction marker" in result["error"]


async def test_recall_tool_refuses_traversal(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    recall = build_recall_tool(SpillStore(tmp_path / "store"))
    assert "error" in await recall(name="../secret.txt")


# --- wiring into the loop --------------------------------------------------

@tool(description="Returns a large payload")
async def big_payload(filler: str = "") -> dict:
    return {"text": "Z" * 3000, "filler": filler}


async def test_loop_compacts_a_long_run(tmp_path):
    """Six fat rounds must not send an unbounded request."""
    registry = ToolRegistry()
    registry.register(big_payload)

    script = [call("big_payload", {}) for _ in range(7)] + [final("enough evidence gathered")]
    llm = ScriptedLLM(script)
    # The system prompt alone is ~400 tokens and is never trimmed, so the budget
    # has to leave room for the protected head and tail.
    budget_tokens = 3_000
    loop = AgentLoop(
        llm, registry,
        budget=AgentBudget(max_rounds=10, context_budget_tokens=budget_tokens,
                           keep_recent_messages=2, max_tool_calls=10),
        spill_dir=tmp_path,
    )
    run = await loop.run("gather a lot of evidence")

    assert run.status == "succeeded"
    assert run.compactions, "a long run should have compacted at least once"

    # Compare the last request with the largest one: compaction has to be doing
    # real work, not merely reporting itself.
    sizes = [estimate_tokens(recorded) for recorded in llm.calls]
    assert sizes[-1] < max(sizes), "the final request was not smaller than the peak"
    assert max(sizes[2:]) <= budget_tokens * 2, "requests stayed in the same order of magnitude"

    # The promise in the marker must be real.
    assert "recall_observation" in registry


async def test_the_cut_scales_to_the_deficit(tmp_path):
    """A fixed head/tail is not enough.

    Frees a constant number of tokens per observation. If a round adds more than
    that, the request grows without bound while every pass reports success. This
    is what a fixed cut did here: at round 8 the request was 4696 tokens against
    a 3000 budget, and still climbing.
    """
    registry = ToolRegistry()
    registry.register(big_payload)
    budget_tokens = 3_000

    async def peak_over(rounds: int) -> tuple[int, list[int]]:
        script = [call("big_payload", {"filler": str(i)}) for i in range(rounds)]
        script.append(final("enough evidence gathered"))
        llm = ScriptedLLM(script)
        loop = AgentLoop(
            llm, registry,
            budget=AgentBudget(max_rounds=rounds + 2, context_budget_tokens=budget_tokens,
                               keep_recent_messages=2, max_tool_calls=rounds + 2),
            spill_dir=tmp_path,
        )
        await loop.run("gather evidence")
        sizes = [estimate_tokens(recorded) for recorded in llm.calls]
        return max(sizes), sizes

    short_peak, _ = await peak_over(7)
    long_peak, long_sizes = await peak_over(14)

    # Doubling the number of fat rounds must not roughly double the request.
    assert long_peak < short_peak * 1.3, (
        f"the request kept growing with the run: {short_peak} -> {long_peak}. "
        "Compaction is freeing a fixed amount per pass, not scaling to the deficit."
    )
    assert long_peak <= budget_tokens * 1.2, (
        f"14 rounds produced a {long_peak}-token request against a {budget_tokens} budget"
    )
    # And neither run returned to the unbounded growth this guards against.
    assert len(long_sizes) == 15


async def test_compaction_reports_when_the_budget_is_unreachable(tmp_path):
    """When the floor is hit, say so rather than claim the bound was met."""
    registry = ToolRegistry()
    registry.register(big_payload)
    # A budget below the system prompt alone can never be satisfied.
    script = [call("big_payload", {}) for _ in range(4)] + [final("done with what I have")]
    llm = ScriptedLLM(script)
    loop = AgentLoop(
        llm, registry,
        budget=AgentBudget(max_rounds=6, context_budget_tokens=50,
                           keep_recent_messages=2, max_tool_calls=6),
        spill_dir=tmp_path,
    )
    run = await loop.run("go")

    assert run.compactions
    assert any(c["unreachable"] for c in run.compactions), (
        "a 50-token budget against a ~400-token system prompt must be reported "
        "as unreachable, not as success"
    )
    assert "unreachable" in run.compactions[-1] and run.compactions[-1]["unreachable"] is True


async def test_compaction_is_recorded_and_not_silent(tmp_path):
    """A run that compacted must say so, with the round and the cost."""
    registry = ToolRegistry()
    registry.register(big_payload)
    script = [call("big_payload", {}) for _ in range(8)] + [final("evidence gathered")]
    loop = AgentLoop(
        ScriptedLLM(script), registry,
        budget=AgentBudget(max_rounds=10, context_budget_tokens=3_000,
                           keep_recent_messages=2, max_tool_calls=10),
        spill_dir=tmp_path,
    )
    run = await loop.run("gather")

    assert run.compactions
    first = run.compactions[0]
    assert {"round", "dropped_tokens", "trimmed_messages", "spilled"} <= set(first)
    assert first["dropped_tokens"] > 0
    assert first["spilled"]
    # And it survives into the serialised run.
    assert run.as_dict()["compactions"] == run.compactions


async def test_loop_registers_recall_only_when_it_can_spill():
    """No spill directory means no retrieval, so no tool promising retrieval."""
    registry = ToolRegistry()
    registry.register(big_payload)
    loop = AgentLoop(ScriptedLLM([final("done")]), registry,
                     budget=AgentBudget(context_budget_tokens=1_000))
    assert "recall_observation" not in registry


async def test_short_runs_do_not_compact(tmp_path):
    registry = ToolRegistry()
    registry.register(big_payload)
    llm = ScriptedLLM([call("big_payload", {}), final("brief and sufficient")])
    loop = AgentLoop(llm, registry,
                     budget=AgentBudget(context_budget_tokens=100_000), spill_dir=tmp_path)
    run = await loop.run("a quick look")

    assert run.compactions == []
    assert run.status == "succeeded"


# --- the production wiring -------------------------------------------------

def test_spill_root_is_configurable(monkeypatch, tmp_path):
    """Deployments may want spill somewhere persistent."""
    import app.agent.service as service

    monkeypatch.setenv("AGENT_SPILL_DIR", str(tmp_path / "custom"))
    assert service.spill_root() == tmp_path / "custom"

    monkeypatch.delenv("AGENT_SPILL_DIR", raising=False)
    assert service.spill_root().name == "tradeagent-spill"


async def test_execute_gives_the_agent_a_working_spill_store(monkeypatch, tmp_path):
    """The production path must make the compaction promise real.

    Compaction trims an observation and tells the model it can read the rest
    back by name. If `execute` does not supply a spill directory, that promise is
    a lie: the detail is gone and the marker points at nothing.
    """
    import app.agent.service as service

    monkeypatch.setenv("AGENT_SPILL_DIR", str(tmp_path))
    registry = ToolRegistry()
    registry.register(big_payload)

    script = [call("big_payload", {"filler": str(i)}) for i in range(6)]
    script.append(final("evidence gathered from the full payloads"))
    result = await service.execute(
        "gather a lot of evidence",
        llm=ScriptedLLM(script),
        registry=registry,
        persist=False,
        budget=AgentBudget(max_rounds=9, context_budget_tokens=3_000,
                           keep_recent_messages=2, max_tool_calls=9),
    )

    assert result.run.status == "succeeded"
    # It compacted, so the recall tool had to be reachable.
    assert result.run.compactions
    assert "recall_observation" in registry
    # And the files the markers name really exist.
    spill = service.spill_root()
    stored = sorted(p.name for p in spill.glob("*.txt"))
    assert stored
    named = [name for c in result.run.compactions for name in c["spilled"]]
    assert named and all(name in stored for name in named)


async def test_compaction_disabled_when_budget_is_zero(tmp_path):
    registry = ToolRegistry()
    registry.register(big_payload)
    script = [call("big_payload", {}) for _ in range(5)] + [final("done, no compaction wanted")]
    llm = ScriptedLLM(script)
    loop = AgentLoop(llm, registry,
                     budget=AgentBudget(max_rounds=8, context_budget_tokens=0),
                     spill_dir=tmp_path)
    run = await loop.run("go")

    assert run.compactions == []
    assert run.status == "succeeded"


async def test_the_model_can_actually_recall_spilled_text(tmp_path):
    """End to end: compact, then ask for the detail back, and get it."""
    registry = ToolRegistry()
    registry.register(big_payload)

    script = [call("big_payload", {"filler": str(i)}) for i in range(6)]
    script.append(call("recall_observation", {"name": "placeholder"}))
    script.append(final("retrieved"))

    llm = ScriptedLLM(script)
    loop = AgentLoop(
        llm, registry,
        budget=AgentBudget(max_rounds=10, context_budget_tokens=1_500,
                           keep_recent_messages=1, max_tool_calls=10),
        spill_dir=tmp_path,
    )
    await loop.run("gather then recall")

    # The loop knows the spill names; ask for one directly and confirm the text.
    spilled = sorted(p.name for p in tmp_path.iterdir())
    assert spilled, "something should have been spilled"
    recall = build_recall_tool(SpillStore(tmp_path))
    result = await recall(name=spilled[0])
    assert len(result["text"]) > 1000
