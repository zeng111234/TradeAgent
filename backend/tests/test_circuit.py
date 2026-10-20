"""The breaker: what counts, when it opens, and who gets the probe.

The behaviour under test is deliberately small -- five consecutive retryable
failures open it, one caller probes after the cooldown, a failed probe doubles
the wait -- because the interesting decisions are about what does **not** count.
A truncated answer, a bad JSON body and a rejected key are not availability
signals, and a breaker that treats them as such takes a healthy provider offline.

Everything here drives the clock explicitly. ``sleep(30)`` in a suite that runs
in under a minute is not a test, it is a payment.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import circuit, llm
from app.config import settings
from app.llm import (
    LLMBadResponse,
    LLMCircuitOpen,
    LLMRateLimited,
    LLMTransient,
    LLMTruncated,
    LLMUnavailable,
)
from app.retry import PERMANENT, classify

KEY = circuit.provider_key(settings.OPENAI_BASE_URL, settings.OPENAI_MODEL)


def _fail(times: int, exc: BaseException | None = None, *, at: float = 0.0,
          key=KEY) -> None:
    for _ in range(times):
        circuit.record_failure(key, exc or LLMTransient("timeout"), now=at)


def _open(*, at: float = 0.0, exc: BaseException | None = None) -> None:
    _fail(circuit.FAILURE_THRESHOLD, exc=exc, at=at)
    assert circuit.state_of(KEY, now=at)["status"] == circuit.OPEN


# --------------------------------------------------------------------------
# Opening
# --------------------------------------------------------------------------

def test_four_failures_are_not_enough_and_the_fifth_opens():
    _fail(4)
    assert circuit.state_of(KEY, now=0)["status"] == circuit.CLOSED
    assert circuit.state_of(KEY, now=0)["consecutive_failures"] == 4

    _fail(1)
    assert circuit.state_of(KEY, now=0)["status"] == circuit.OPEN


def test_an_open_circuit_refuses_before_the_call_is_attempted():
    _open()

    with pytest.raises(LLMCircuitOpen) as caught:
        circuit.before_call(KEY, now=1.0)

    message = str(caught.value)
    assert "paused until" in message
    assert "last failure" in message
    assert caught.value.retry_in == pytest.approx(29.0)
    assert caught.value.retry_at is not None


def test_the_refusal_is_permanent_for_the_retry_policy():
    """Explicit in ``classify``, not inherited from its default branch."""
    decision = classify(LLMCircuitOpen("open"))
    assert decision.kind == PERMANENT
    assert not decision.retryable


def test_truncation_is_not_evidence_of_unavailability():
    """A long prompt is cut on a healthy provider. Counting it would be a bug."""
    _fail(20, exc=LLMTruncated("length"))
    assert circuit.state_of(KEY, now=0)["status"] == circuit.CLOSED


def test_a_rejected_key_and_a_bad_body_do_not_open_it():
    _fail(20, exc=LLMUnavailable("no key"))
    _fail(20, exc=LLMBadResponse("not json"))
    assert circuit.state_of(KEY, now=0)["status"] == circuit.CLOSED
    assert circuit.state_of(KEY, now=0)["consecutive_failures"] == 0


def test_a_non_retryable_failure_breaks_the_consecutive_chain():
    _fail(4)
    circuit.record_failure(KEY, LLMTruncated("length"))
    _fail(4)
    assert circuit.state_of(KEY, now=0)["status"] == circuit.CLOSED

    _fail(1)
    assert circuit.state_of(KEY, now=0)["status"] == circuit.OPEN


def test_a_rate_limit_sets_the_cooldown_floor():
    _open(exc=LLMRateLimited("429", retry_after=45.0))

    with pytest.raises(LLMCircuitOpen):
        circuit.before_call(KEY, now=44.9)
    circuit.before_call(KEY, now=45.0)  # exactly at the provider's own answer


# --------------------------------------------------------------------------
# Half-open: exactly one probe
# --------------------------------------------------------------------------

def test_after_the_cooldown_exactly_one_caller_gets_the_probe():
    _open()

    with pytest.raises(LLMCircuitOpen):
        circuit.before_call(KEY, now=29.9)

    circuit.before_call(KEY, now=30.0)  # this caller is the probe

    with pytest.raises(LLMCircuitOpen) as caught:
        circuit.before_call(KEY, now=30.0)
    assert "probe is already in flight" in str(caught.value)

    circuit.record_success(KEY)
    assert circuit.state_of(KEY, now=30.0)["status"] == circuit.CLOSED
    circuit.before_call(KEY, now=30.0)  # closed: everyone may proceed


def test_a_failed_probe_reopens_and_doubles_the_wait():
    _open()
    circuit.before_call(KEY, now=30.0)
    circuit.record_failure(KEY, LLMTransient("still down"), now=30.0)

    state = circuit.state_of(KEY, now=30.0)
    assert state["status"] == circuit.OPEN
    assert state["cooldown"] == 60.0

    with pytest.raises(LLMCircuitOpen):
        circuit.before_call(KEY, now=89.9)
    circuit.before_call(KEY, now=90.0)


def test_the_doubled_wait_is_capped():
    _open()  # opened at 0, cooldown 30

    now = 30.0
    for expected in (60.0, 120.0, 240.0, 300.0, 300.0):
        circuit.before_call(KEY, now=now)  # the probe, allowed at the boundary
        circuit.record_failure(KEY, LLMTransient("still down"), now=now)
        state = circuit.state_of(KEY, now=now)
        assert state["status"] == circuit.OPEN
        assert state["cooldown"] == expected
        now += expected


def test_a_cancelled_probe_does_not_wedge_the_breaker():
    """A disconnect during the trial must return the slot, not keep it forever."""
    _open()
    circuit.before_call(KEY, now=30.0)
    circuit.release_trial(KEY)
    circuit.before_call(KEY, now=30.0)  # the slot is available again


# --------------------------------------------------------------------------
# The seam calls it: a real failure path, not just the module in isolation
# --------------------------------------------------------------------------

class _Completions:
    def __init__(self, error: BaseException | None = None):
        self.calls = 0
        self.error = error

    async def create(self, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="ok"),
            )],
            usage=None,
        )


class _Client:
    def __init__(self, error: BaseException | None = None):
        self.chat = SimpleNamespace(completions=_Completions(error))


def _complete(error: BaseException | None = None):
    return asyncio.run(llm.complete_text("hello"))


def test_five_failures_through_complete_text_stop_reaching_the_client(monkeypatch):
    fake = _Client(LLMTransient("the provider returned HTTP 503"))
    monkeypatch.setattr(llm, "get_client", lambda: fake)

    for _ in range(circuit.FAILURE_THRESHOLD):
        with pytest.raises(LLMTransient):
            _complete()

    calls_before = fake.chat.completions.calls
    with pytest.raises(LLMCircuitOpen):
        _complete()

    assert fake.chat.completions.calls == calls_before, (
        "the open circuit still reached the provider; every refused call would "
        "cost another timeout"
    )


def test_a_bug_of_ours_is_neither_counted_nor_a_stuck_probe(monkeypatch):
    fake = _Client(TypeError("our own mistake"))
    monkeypatch.setattr(llm, "get_client", lambda: fake)

    with pytest.raises(TypeError):
        _complete()

    assert circuit.state_of(KEY)["status"] == circuit.CLOSED
    assert circuit.state_of(KEY)["consecutive_failures"] == 0


def test_both_seams_share_one_state():
    """An outage seen by the legacy path is knowledge the runtime path has."""
    from app.agent.protocol import OpenAICompatibleClient

    class _Inner:
        def __init__(self):
            self.calls = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(
                create=self._create
            ))

        async def _create(self, **_kwargs):
            self.calls += 1
            raise LLMTransient("the provider returned HTTP 503")

    client = OpenAICompatibleClient(
        api_key="test", base_url=settings.OPENAI_BASE_URL,
        model=settings.OPENAI_MODEL,
    )
    inner = _Inner()
    client._client = inner

    for _ in range(circuit.FAILURE_THRESHOLD):
        with pytest.raises(LLMTransient):
            asyncio.run(client.chat([], []))

    calls_before = inner.calls
    with pytest.raises(LLMCircuitOpen):
        asyncio.run(client.chat([], []))
    assert inner.calls == calls_before

    # The same key through the other seam is refused too, without touching a
    # client at all.
    with pytest.raises(LLMCircuitOpen):
        circuit.before_call(KEY)
