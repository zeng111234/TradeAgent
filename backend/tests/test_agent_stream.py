"""Streaming: what the provider adapter assembles, and what the stream emits.

Three properties this file exists to protect:

* a streamed reply must equal a non-streamed one -- same text, same tool calls,
  same usage -- or the trace becomes a different record depending on transport;
* streaming degrades without lying: a rejected option is dropped, a provider
  that cannot stream falls back once, and a missing usage block is *unknown*
  rather than zero;
* ``live``/``delta`` frames carry no SSE ``id``, because ``id`` is the resume
  cursor and a partial round must not move it.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from types import SimpleNamespace

import pytest

from app.agent import live as live_bus
from app.agent.protocol import OpenAICompatibleClient


# --- a fake provider stream ------------------------------------------------


class FakeStream:
    def __init__(self, chunks, *, fail_at=None):
        self._chunks = list(chunks)
        self._index = 0
        self._fail_at = fail_at
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._fail_at is not None and self._index == self._fail_at:
            raise RuntimeError("connection reset by peer")
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def close(self):
        self.closed = True


def make_chunk(*, content=None, reasoning=None, tool_calls=None, finish=None, usage=None, empty=False):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )
    choices = [] if empty else [SimpleNamespace(delta=delta, finish_reason=finish)]
    return SimpleNamespace(choices=choices, usage=usage)


def make_client(factory):
    """A client whose provider is a scripted async iterator factory."""
    client = OpenAICompatibleClient(
        # A unique base URL per test: the circuit breaker keys on it, and one
        # test's induced failure must not open the breaker for the next.
        api_key="test-key",
        base_url=f"https://example.test/{uuid.uuid4().hex}/v1",
        model="test-model",
    )

    class Completions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            value = factory(kwargs, len(self.calls))
            if inspect.isawaitable(value):
                value = await value
            return value

    completions = Completions()
    client._ensure_client = lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return client, completions


def usage_chunk(prompt=10, completion=4, cached=2):
    return make_chunk(
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
        empty=True,
    )


# --- the adapter -----------------------------------------------------------


def test_a_streamed_reply_equals_the_assembled_one():
    fragments = [
        SimpleNamespace(index=0, id="call_1", function=SimpleNamespace(name="lookup", arguments="")),
        SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments='{"key":')),
        SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments=' "yarn"}')),
    ]
    chunks = [
        make_chunk(reasoning="I should look first."),
        make_chunk(content="Hel"),
        make_chunk(content="lo"),
        make_chunk(tool_calls=fragments[:1]),
        make_chunk(tool_calls=fragments[1:]),
        make_chunk(finish="tool_calls"),
        usage_chunk(),
    ]
    deltas: list[tuple[str, str]] = []

    client, calls = make_client(lambda _kwargs, _n: FakeStream(chunks))
    reply = asyncio.run(
        client.chat([], [], on_delta=lambda kind, text: deltas.append((kind, text)))
    )

    assert reply.content == "Hello"
    assert reply.reasoning == "I should look first."
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0].name == "lookup"
    assert reply.tool_calls[0].arguments == '{"key": "yarn"}'
    assert reply.finish_reason == "tool_calls"
    assert (reply.prompt_tokens, reply.completion_tokens, reply.cached_prompt_tokens) == (10, 4, 2)
    assert reply.usage_reported is True

    # Reasoning first, then the answer, in the order the provider produced it.
    assert deltas == [
        ("reasoning", "I should look first."),
        ("content", "Hel"),
        ("content", "lo"),
    ]
    # Usage is requested, because a streamed run that cannot count its tokens
    # cannot be priced.
    assert calls.calls[0]["stream"] is True
    assert calls.calls[0]["stream_options"] == {"include_usage": True}


def test_stream_options_is_dropped_rather_than_giving_up_streaming():
    def factory(_kwargs, nth):
        if nth == 1:
            raise RuntimeError("Unsupported parameter: stream_options")
        return FakeStream([make_chunk(content="ok"), usage_chunk()])

    client, calls = make_client(factory)
    reply = asyncio.run(client.chat([], [], on_delta=lambda *_: None))

    assert reply.content == "ok"
    assert "stream_options" in calls.calls[0]
    assert "stream_options" not in calls.calls[1]
    assert client._include_usage is False


def test_a_stream_that_fails_before_the_first_chunk_falls_back_for_that_round_only():
    """One dropped socket is weather, not a verdict on the endpoint.

    The first version treated any pre-first-chunk failure as "this provider
    cannot stream" and disabled the feature for the rest of the run. A real
    DeepSeek run hit a transient connection error on round 1 and spent round 2
    -- the round that actually answered -- with no deltas at all.
    """
    def factory(_kwargs, nth):
        if nth == 1:
            return FakeStream([make_chunk(content="never seen")], fail_at=0)
        if nth == 2:
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="from the non-streaming retry", tool_calls=None),
                )],
                usage=None,
            )
        return FakeStream([make_chunk(content="streamed again"), usage_chunk()])

    client, calls = make_client(factory)
    deltas: list[str] = []

    first = asyncio.run(client.chat([], [], on_delta=lambda _k, text: deltas.append(text)))

    assert first.content == "from the non-streaming retry"
    assert client.supports_stream is True, (
        "a transport failure must not disable streaming for the whole run"
    )
    assert len(calls.calls) == 2

    second = asyncio.run(client.chat([], [], on_delta=lambda _k, text: deltas.append(text)))

    assert second.content == "streamed again"
    assert deltas == ["streamed again"]
    assert calls.calls[-1]["stream"] is True


def test_a_provider_that_rejects_streaming_is_not_asked_again():
    """A 4xx about the request shape is a durable answer about the endpoint."""

    class BadRequest(Exception):
        status_code = 400

    def factory(kwargs, _nth):
        if kwargs.get("stream"):
            raise BadRequest("this endpoint does not support stream=true")
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="plain answer", tool_calls=None),
            )],
            usage=None,
        )

    client, calls = make_client(factory)

    asyncio.run(client.chat([], [], on_delta=lambda *_: None))
    assert client.supports_stream is False

    # The second round goes straight to the non-streaming path: no second
    # doomed attempt, which is the whole reason to remember the refusal.
    asyncio.run(client.chat([], [], on_delta=lambda *_: None))
    attempts = [call for call in calls.calls if call.get("stream")]
    assert len(attempts) == 1, "the rejected stream was attempted again"
    assert len(calls.calls) == 3


def test_two_empty_streams_disable_streaming_for_the_run():
    """Accepted-then-closed is tolerated once, then treated as capability."""

    def factory(kwargs, _nth):
        if kwargs.get("stream"):
            return FakeStream([])
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="answer without deltas", tool_calls=None),
            )],
            usage=None,
        )

    client, _calls = make_client(factory)

    asyncio.run(client.chat([], [], on_delta=lambda *_: None))
    assert client.supports_stream is True, "one empty stream is a hiccup"

    asyncio.run(client.chat([], [], on_delta=lambda *_: None))
    assert client.supports_stream is False


def test_a_stream_that_fails_after_output_is_not_retried():
    """Re-sending would duplicate the answer and bill it twice."""
    client, calls = make_client(
        lambda _kwargs, _n: FakeStream([make_chunk(content="partial")], fail_at=1)
    )

    with pytest.raises(Exception):
        asyncio.run(client.chat([], [], on_delta=lambda *_: None))

    assert len(calls.calls) == 1


def test_a_stream_without_usage_is_unknown_not_zero():
    client, _ = make_client(
        lambda _kwargs, _n: FakeStream([make_chunk(content="answer")])
    )
    reply = asyncio.run(client.chat([], [], on_delta=lambda *_: None))

    assert reply.content == "answer"
    assert reply.usage_reported is False
    assert reply.prompt_tokens == 0


def test_no_sink_means_no_stream():
    """A caller that does not render deltas has no reason to pay for them."""
    client, calls = make_client(
        lambda _kwargs, _n: SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="plain", tool_calls=None),
            )],
            usage=None,
        )
    )
    reply = asyncio.run(client.chat([], []))

    assert reply.content == "plain"
    assert "stream" not in calls.calls[0]


# --- the live buffer -------------------------------------------------------


def test_live_state_is_authoritative_and_deltas_are_incremental():
    live_bus.close_run("r1")
    live_bus.open_run("r1")
    live_bus.begin_round("r1", 1)
    sink = live_bus.sink("r1")

    sink("reasoning", "think")
    sink("content", "Hel")
    sink("content", "lo")

    state = live_bus.state("r1")
    assert state["round"] == 1
    assert state["content"] == "Hello"
    assert state["reasoning"] == "think"
    assert [d["text"] for d in live_bus.since("r1", 0)] == ["think", "Hel", "lo"]
    assert [d["seq"] for d in live_bus.since("r1", 1)] == [2, 3]

    live_bus.begin_round("r1", 2)
    assert live_bus.state("r1")["content"] == ""
    assert live_bus.state("r1")["round"] == 2

    live_bus.close_run("r1")
    assert live_bus.state("r1") is None
    assert live_bus.since("r1", 0) == []


def test_a_reader_behind_the_ring_is_told_to_resync():
    live_bus.close_run("r2")
    run = live_bus.open_run("r2")
    run.emit("content", "a")
    run.dropped_before_seq = 5

    assert run.since(0) == []
    assert run.state()["content"] == "a"


# --- the SSE frames --------------------------------------------------------


def read_events(client, url: str, *, limit: int = 80) -> list[dict]:
    events: list[dict] = []
    current = {"event": "message", "id": None, "data": None}
    with client.stream("GET", url, timeout=15) as response:
        assert response.status_code == 200, response.text
        for line in response.iter_lines():
            if line == "":
                if current["data"] is not None:
                    events.append(current)
                    current = {"event": "message", "id": None, "data": None}
                    if len(events) >= limit:
                        break
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value.lstrip(" ")
            if field == "event":
                current["event"] = value
            elif field == "id":
                current["id"] = int(value)
            elif field == "data":
                current["data"] = json.loads(value)
    return events


def test_live_and_delta_frames_carry_no_resume_id(client, monkeypatch):
    """``id`` is the round cursor; a partial round must not advance it."""
    import app.routers.agent_runs as routes
    from app.agent.loop import AgentRun, TraceStep
    from app.agent.service import AgentResult

    async def fake_execute(goal, *, run_id=None, on_step=None, on_delta=None, on_round=None, **kwargs):
        run = AgentRun(goal=goal, run_id=run_id)
        on_round(1)
        on_delta("reasoning", "checking the book")
        await asyncio.sleep(0.25)
        on_delta("content", "Hel")
        await asyncio.sleep(0.25)
        on_delta("content", "lo")
        step = TraceStep(round=1, thought="Hello", reasoning="checking the book")
        run.steps.append(step)
        if on_step:
            on_step(step, run)
        await asyncio.sleep(0.25)
        run.status = "succeeded"
        run.stop_reason = "final answer"
        run.final_output = "Hello"
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", fake_execute)

    started = client.post("/api/v1/agent/run", json={"goal": "stream me"}).json()
    events = read_events(client, f"/api/v1/agent/runs/{started['run_id']}/events")

    names = [event["event"] for event in events]
    assert "live" in names
    assert "delta" in names
    assert names[-1] == "done"

    for event in events:
        if event["event"] in ("live", "delta"):
            assert event["id"] is None, f"{event['event']} must not move the cursor"
        else:
            assert event["id"] is not None

    live = next(event for event in events if event["event"] == "live")
    assert live["data"]["round"] == 1
    assert live["data"]["reasoning"] == "checking the book"

    delta = next(event for event in events if event["event"] == "delta")
    assert delta["data"]["kind"] in ("content", "reasoning")
    assert "text" in delta["data"]
