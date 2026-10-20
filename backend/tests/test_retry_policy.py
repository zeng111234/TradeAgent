"""Which failures are worth another try, and which are not.

Retrying is a judgement about *why* something failed, and it is only safe when the
operation can be repeated. Getting it wrong in either direction costs:

* retrying the wrong thing -- a rejected key, a malformed request, our own bug --
  multiplies a failure that cannot succeed, and turns "the credentials are wrong"
  into "the service is slow". The SDK has already retried it twice by then.
* not retrying the right thing -- a dropped connection, a 5xx, a rate limit --
  loses work that one second would have saved.

There is a third category which is neither, and it is the one people get wrong: a
failure that *can* be tried again but **not with the same inputs**. A truncated
answer retried at the same token cap is a guaranteed repeat; a JSON parse failure
retried at the same temperature is the same coin flip. Those are ``repair``, and
the policy says so separately from ``retry``.
"""
from __future__ import annotations

import time

import pytest

from app import retry
from app.llm import (
    LLMBadResponse,
    LLMRateLimited,
    LLMTransient,
    LLMTruncated,
    LLMUnavailable,
)


@pytest.fixture(autouse=True)
def _allow_the_fake_host(monkeypatch):
    """The fetch under test is faked; DNS for ``example.test`` is not.

    ``fetch_public`` refuses a host that does not resolve, which is correct in
    production and meaningless here: the host exists only in the test. The
    address policy has its own file (``test_url_guard.py``); this one is about
    retrying a transport failure.
    """
    from app import url_guard

    monkeypatch.setattr(url_guard, "ensure_public_http_url", lambda url: url)


# --------------------------------------------------------------------------
# The judgement
# --------------------------------------------------------------------------

def test_a_transient_provider_failure_is_retried():
    decision = retry.classify(LLMTransient("the provider returned HTTP 503"))

    assert decision.kind == retry.RETRY
    assert decision.wait_seconds > 0


def test_a_rate_limit_is_retried():
    assert retry.classify(LLMRateLimited("429")).kind == retry.RETRY


def test_a_rate_limit_waits_for_as_long_as_the_provider_asked():
    """The one number a retry decision needs, and it used to be thrown away."""
    decision = retry.classify(LLMRateLimited("429", retry_after=12.0))

    assert decision.kind == retry.RETRY
    assert decision.wait_seconds == 12.0, (
        "our own guess is strictly worse than the provider's answer"
    )


def test_a_rate_limit_longer_than_a_request_can_wait_is_not_retried_here():
    """An hour-long ``Retry-After`` is not a retry, it is a hang."""
    decision = retry.classify(LLMRateLimited("429", retry_after=3600.0))

    assert decision.kind == retry.PERMANENT
    assert "longer than a request can wait" in decision.reason


def test_a_rate_limit_without_a_hint_still_gets_a_backoff():
    decision = retry.classify(LLMRateLimited("429", retry_after=None))

    assert decision.kind == retry.RETRY
    assert 0 <= decision.wait_seconds <= retry.MAX_DELAY_SECONDS


@pytest.mark.parametrize(
    "error",
    [
        LLMUnavailable("no key"),
        LLMUnavailable("the provider rejected the credentials (AuthenticationError, HTTP 401)"),
    ],
)
def test_credentials_are_never_retried(error):
    """A rejected key does not become accepted by asking again.

    This is also why the SDK's three attempts are the *only* ones: adding a layer
    here would turn a configuration mistake into six requests and a long wait.
    """
    decision = retry.classify(error)

    assert decision.kind == retry.PERMANENT
    assert not decision.retryable


def test_truncation_is_repaired_not_retried():
    """Same prompt, same cap, same cut. A repeat would be a wasted request."""
    decision = retry.classify(LLMTruncated("stopped at the token cap"))

    assert decision.kind == retry.REPAIR, (
        "retrying truncation unchanged is a guaranteed repeat"
    )
    assert "same cap" in decision.reason


def test_an_unusable_answer_may_be_sampled_again():
    decision = retry.classify(LLMBadResponse("provider did not return valid JSON"))

    assert decision.kind == retry.REPAIR


@pytest.mark.parametrize(
    "error",
    [ValueError("our own bug"), TypeError("also ours"), KeyError("ours as well")],
)
def test_our_own_bugs_are_not_retried(error):
    """The important default.

    An unrecognised exception is usually a defect. Retrying a defect is how it
    becomes a timeout instead of a traceback.
    """
    decision = retry.classify(error)

    assert decision.kind == retry.PERMANENT
    assert "our own bug" in decision.reason


def test_every_decision_carries_a_reason():
    """A retry nobody can explain is indistinguishable from a guess."""
    for error in (
        LLMTransient("x"), LLMRateLimited("y"), LLMTruncated("z"),
        LLMUnavailable("w"), LLMBadResponse("v"), ValueError("u"),
    ):
        assert retry.classify(error).reason


# --------------------------------------------------------------------------
# HTTP status codes, for clients with no exception taxonomy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_server_side_and_throttling_statuses_are_retryable(status):
    assert retry.classify_http_status(status).retryable


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_side_statuses_are_permanent(status):
    """A 404 is an answer, not an accident."""
    assert retry.classify_http_status(status).kind == retry.PERMANENT


# --------------------------------------------------------------------------
# Backoff shape
# --------------------------------------------------------------------------

def test_backoff_grows_then_stops_growing():
    waits = [retry.backoff_seconds(n, jitter=False) for n in range(1, 10)]

    assert waits[:4] == sorted(waits[:4]), f"not increasing: {waits}"
    assert max(waits) <= retry.MAX_DELAY_SECONDS, (
        "an uncapped backoff eventually exceeds every budget in app/timeouts.py"
    )


def test_backoff_is_jittered():
    """Ten workers rate-limited at once must not return at once."""
    samples = {round(retry.backoff_seconds(3), 6) for _ in range(50)}

    assert len(samples) > 1, "a fixed delay turns a rate limit into a thundering herd"


def test_backoff_is_bounded_by_its_ceiling_below_the_cap():
    for _ in range(50):
        assert 0 <= retry.backoff_seconds(2) <= 2.0


# --------------------------------------------------------------------------
# The helper that runs the loop
# --------------------------------------------------------------------------

def test_the_helper_retries_and_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    calls = []

    def flaky():
        calls.append(len(calls) + 1)
        if len(calls) < 2:
            raise LLMTransient("first one failed")
        return "ok"

    assert retry.run_with_retries(flaky, attempts=2) == "ok"
    assert len(calls) == 2


def test_the_helper_gives_up_after_the_attempt_count(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def always_fails():
        raise LLMTransient("nope")

    with pytest.raises(LLMTransient):
        retry.run_with_retries(always_fails, attempts=2)


def test_the_helper_does_not_retry_a_permanent_failure(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    calls = []

    def refused():
        calls.append(1)
        raise LLMUnavailable("no key")

    with pytest.raises(LLMUnavailable):
        retry.run_with_retries(refused, attempts=5)

    assert len(calls) == 1, "credentials were retried; five attempts for one answer"


def test_the_helper_will_not_wait_past_the_deadline(monkeypatch):
    """A retry that sleeps past the budget is a slower way of failing."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    calls = []

    def fails_slowly():
        calls.append(1)
        raise LLMTransient("still failing")

    deadline = time.monotonic() + 0.01
    monkeypatch.setattr(retry, "backoff_seconds", lambda *_a, **_k: 60.0)

    with pytest.raises(LLMTransient):
        retry.run_with_retries(fails_slowly, attempts=5, deadline=deadline)

    assert len(calls) == 1, "it retried into a budget it could not fit inside"


def test_retry_only_narrows_what_may_be_retried(monkeypatch):
    """An operation whose failures are mostly permanent should say so."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    calls = []

    def refuses():
        calls.append(1)
        raise OSError("something else entirely")

    with pytest.raises(OSError):
        retry.run_with_retries(refuses, attempts=3, retry_only=(LLMTransient,))

    assert len(calls) == 1


# --------------------------------------------------------------------------
# The page fetch, which had no retry at all
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, text="<html><title>t</title><p>x</p></html>"):
        self.status_code = status_code
        self.text = text


def _fetch_with(monkeypatch, responses):
    """Run inspect_website with a scripted sequence of requests outcomes."""
    import asyncio

    import requests

    from app.agent.trade_tools import inspect_website

    calls = []

    def fake_get(*_args, **_kwargs):
        outcome = responses[min(len(calls), len(responses) - 1)]
        calls.append(outcome)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    result = asyncio.run(inspect_website(url="https://example.test"))
    return result, calls


def test_a_transient_network_error_is_retried_once(monkeypatch):
    result, calls = _fetch_with(
        monkeypatch, [OSError("connection reset"), FakeResponse(200)]
    )

    assert len(calls) == 2, "a reset connection is a page one more try would fetch"
    assert result["status_code"] == 200
    assert "error" not in result


def test_a_404_phase_is_not_retried(monkeypatch):
    """It will be 404 again, and the second request costs a second of the budget."""
    result, calls = _fetch_with(monkeypatch, [FakeResponse(404)])

    assert len(calls) == 1
    assert result["status_code"] == 404


def test_a_503_is_retried(monkeypatch):
    result, calls = _fetch_with(monkeypatch, [FakeResponse(503), FakeResponse(200)])

    assert len(calls) == 2
    assert result["status_code"] == 200


def test_the_fetch_stops_after_two_attempts(monkeypatch):
    result, calls = _fetch_with(monkeypatch, [OSError("down"), OSError("still down")])

    assert len(calls) == 2, "an unbounded retry loop inside a tool call"
    assert "error" in result
    assert "OSError" in result["error"]
