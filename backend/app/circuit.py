"""A circuit breaker that consumes ``retry.classify`` instead of inventing a taxonomy.

Why this exists at all
----------------------
A daily-use tool does not need a breaker to survive load. It needs one for a
narrower reason: **a provider outage should become shared knowledge inside the
process.** Today a 429's ``retry_after`` only governs the call that received it;
the next call -- another feature, another run -- knows nothing and burns its own
three attempts. Five surfaces failing in a row is the cheapest possible way to
learn "the provider is down", and the error a human reads should say that once,
not fifteen times.

The rules, and the reasons they are rules
-----------------------------------------
* **Only ``classify(exc).kind == RETRY`` counts as evidence.** A truncated answer
  is input-dependent (a long prompt is cut on a healthy provider), a JSON parse
  failure is a coin flip, and a rejected key needs a person. Counting any of them
  would let a healthy provider trip the breaker or hide a configuration error
  behind "provider unavailable". ``app/retry.py`` already made these distinctions;
  re-deciding them here is how two taxonomies start disagreeing.
* **Consecutive failures, not an error rate.** With tens of calls a day, a
  sliding window spans days and measures nothing. Consecutive is the only
  meaningful primitive at this volume.
* **One probe, not a wave.** When the cooldown expires, exactly one caller gets a
  trial; everyone else keeps failing fast. Setting the state to ``half_open``
  without claiming the trial would turn the first moment of recovery into a
  small flood.
* **In-process memory.** A restart is a fresh observation, not lost state. A
  persisted open circuit can outlive the outage it described, and nobody thinks
  to go and clear it.
* **Keyed by ``(base_url, model)``.** One model failing should not stop calls to
  another. Timeouts and retry counts are deliberately not part of the key: they
  configure how a call is made, not whether the provider is available.

The lock is a ``threading.Lock``, the same choice as ``llm_usage._LOCK`` and
``_SEND_LOCK`` -- these calls happen on worker threads as well as the event
loop, and an ``asyncio.Lock`` is bound to one loop.

What this deliberately does not do: per-customer breakers (there are no users),
distributed state (single process; with several workers each process would keep
its own, which is a caveat to state rather than a thing to paper over), or
per-domain breakers for page fetches (that needs bounded memory, and a dead
domain already fails its own candidate without affecting the next).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from app.retry import RETRY, classify

#: Surface failures needed to open. Each one already contains the SDK's three
#: HTTP attempts (1 + 2 retries), so five surfaces is roughly fifteen requests --
#: enough to distinguish an outage from a wobble.
FAILURE_THRESHOLD = 5

#: How long to stay open before allowing one probe. A person clicking a button
#: can wait thirty seconds; the CI job's ceiling is fifteen minutes.
OPEN_SECONDS = 30.0

#: Doubling stops here. Uncapped exponential backoff eventually exceeds every
#: budget in ``app/timeouts.py``, at which point "recovery" is slower than
#: failing.
MAX_OPEN_SECONDS = 300.0

#: Exactly one trial. Multiple concurrent probes are a flood with a nicer name.
CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

ProviderKey = Tuple[str, str]

_LOCK = threading.Lock()
_STATES: Dict[ProviderKey, "_State"] = {}


@dataclass
class _State:
    status: str = CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    cooldown: float = OPEN_SECONDS
    #: Exactly one trial may be in flight. Set inside the lock by ``before_call``
    #: (test-and-set); cleared by success, failure, or an abandonment.
    trial_in_flight: bool = False
    last_error: str = ""
    last_failure_at: Optional[float] = None

    # --- counters, because a threshold nobody measures is a guess -----------
    # ``FAILURE_THRESHOLD`` and ``OPEN_SECONDS`` were design values: five
    # failures and thirty seconds, chosen from the call volume rather than from
    # an observed outage. These four make the design falsifiable -- an outage
    # that opens the circuit eleven times in a minute says the cooldown is too
    # short, and one that never opens while hundreds of calls fail says the
    # threshold is counting the wrong thing.
    opened_total: int = 0
    half_open_total: int = 0
    rejected_total: int = 0
    failures_total: int = 0


def provider_key(base_url: Optional[str], model: Optional[str]) -> ProviderKey:
    """The identity of a provider for breaker purposes.

    Both seams (``app.llm`` and ``app.agent.protocol``) derive it the same way,
    so an open circuit on the runtime path is visible to the legacy path -- they
    talk to the same endpoint and the same model.
    """
    return (base_url or "", model or "")


def _state_for(key: ProviderKey) -> _State:
    state = _STATES.get(key)
    if state is None:
        state = _State()
        _STATES[key] = state
    return state


def _now() -> float:
    """Monotonic seconds, injectable through the ``now`` parameters below."""
    return time.monotonic()


def _circuit_open_error(state: _State, now: float, *, probing: bool = False):
    # Imported lazily: ``app.llm`` imports this module, and the exception family
    # lives there. A module-level import would be a cycle.
    from app.llm import LLMCircuitOpen

    if probing:
        return LLMCircuitOpen(
            "the model provider has been failing repeatedly; a health probe is "
            "already in flight, so this call was not started"
        )

    retry_in = max(0.0, (state.opened_at + state.cooldown) - now)
    retry_at = datetime.now() + timedelta(seconds=retry_in)
    last = f"; last failure: {state.last_error}" if state.last_error else ""
    return LLMCircuitOpen(
        "the model provider has been failing repeatedly; calls are paused until "
        f"{retry_at:%H:%M:%S} ({retry_in:.0f}s){last}",
        retry_at=retry_at,
        retry_in=retry_in,
    )


def before_call(key: ProviderKey, *, now: Optional[float] = None) -> None:
    """Raise ``LLMCircuitOpen`` when this call must not be attempted.

    In ``half_open`` this also claims the single trial slot under the lock. The
    test-and-set is the point: checking the state and then deciding would let two
    callers both believe they are the probe.
    """
    current = _now() if now is None else now

    with _LOCK:
        state = _STATES.get(key)
        if state is None or state.status == CLOSED:
            return

        if state.status == OPEN:
            if current < state.opened_at + state.cooldown:
                state.rejected_total += 1
                raise _circuit_open_error(state, current)
            state.status = HALF_OPEN
            state.trial_in_flight = False

        if state.trial_in_flight:
            state.rejected_total += 1
            raise _circuit_open_error(state, current, probing=True)

        state.trial_in_flight = True
        state.half_open_total += 1


def record_success(key: ProviderKey, *, now: Optional[float] = None) -> None:
    """The provider answered. Close, clear the count, reset the cooldown."""
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            return
        state.status = CLOSED
        state.consecutive_failures = 0
        state.cooldown = OPEN_SECONDS
        state.trial_in_flight = False
        state.last_error = ""


def release_trial(key: ProviderKey) -> None:
    """Give up a claimed trial without judging the provider.

    Used when the call is abandoned for a reason that says nothing about
    availability -- cancellation, or a bug in our own code. Without this, a
    cancelled probe would leave ``trial_in_flight`` set forever and every later
    call would be refused as "a probe is already in flight": a stuck breaker
    caused by a client disconnect.
    """
    with _LOCK:
        state = _STATES.get(key)
        if state is not None:
            state.trial_in_flight = False


def record_failure(key: ProviderKey, exc: BaseException, *,
                   now: Optional[float] = None) -> None:
    """Feed one failure to the breaker.

    Non-``RETRY`` failures are **not** evidence of unavailability. They break the
    consecutive chain (the provider did answer) and, if they arrive during a
    trial, close the circuit: reachability was demonstrated even though the
    answer was unusable. This keeps the breaker's vocabulary exactly aligned with
    ``retry.classify``.
    """
    current = _now() if now is None else now
    decision = classify(exc)

    with _LOCK:
        state = _STATES.get(key)
        if decision.kind != RETRY:
            if state is None:
                return
            if state.status == HALF_OPEN:
                state.status = CLOSED
                state.cooldown = OPEN_SECONDS
            state.consecutive_failures = 0
            state.trial_in_flight = False
            return

        state = _state_for(key)
        state.last_error = str(exc)[:200]
        state.last_failure_at = current
        state.failures_total += 1

        if state.status == OPEN:
            # A call that began before the circuit opened, finishing late. Do not
            # extend the cooldown: repeated late failures would postpone the probe
            # forever.
            state.trial_in_flight = False
            return

        if state.status == HALF_OPEN:
            state.status = OPEN
            state.opened_total += 1
            state.cooldown = min(max(state.cooldown, OPEN_SECONDS) * 2,
                                 MAX_OPEN_SECONDS)
            state.opened_at = current
            state.trial_in_flight = False
            return

        state.consecutive_failures += 1
        if state.consecutive_failures >= FAILURE_THRESHOLD:
            state.status = OPEN
            state.opened_total += 1
            state.opened_at = current
            state.cooldown = _opening_cooldown(exc)
            state.trial_in_flight = False


def _opening_cooldown(exc: BaseException) -> float:
    """Stay open at least as long as the provider asked us to.

    A rate limit with ``Retry-After: 45`` is the provider's own answer to "when";
    probing at thirty seconds would be told no again. Capped, because an
    hour-long header is not a cooldown, it is a reason to stop calling.
    """
    from app.llm import LLMRateLimited

    if isinstance(exc, LLMRateLimited):
        given = getattr(exc, "retry_after", None)
        if given is not None and given > 0:
            return min(max(OPEN_SECONDS, float(given)), MAX_OPEN_SECONDS)
    return OPEN_SECONDS


def state_of(key: ProviderKey, *, now: Optional[float] = None) -> dict:
    """A read-only view, for tests and for anyone debugging a stuck circuit."""
    current = _now() if now is None else now
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            return {"status": CLOSED, "consecutive_failures": 0,
                    "cooldown": OPEN_SECONDS, "retry_in": 0.0,
                    "last_error": ""}
        retry_in = 0.0
        if state.status == OPEN:
            retry_in = max(0.0, (state.opened_at + state.cooldown) - current)
        return {
            "status": state.status,
            "consecutive_failures": state.consecutive_failures,
            "cooldown": state.cooldown,
            "retry_in": round(retry_in, 3),
            "last_error": state.last_error,
            "opened_total": state.opened_total,
            "half_open_total": state.half_open_total,
            "rejected_total": state.rejected_total,
            "failures_total": state.failures_total,
        }


def reset() -> None:
    """Forget every circuit. Tests call this between cases."""
    with _LOCK:
        _STATES.clear()


def snapshot() -> dict:
    """Every known provider and its state, for a debug endpoint or a log line."""
    with _LOCK:
        return {
            f"{base_url or '(default)'}|{model or '(default)'}": {
                "status": state.status,
                "consecutive_failures": state.consecutive_failures,
                "cooldown": state.cooldown,
                "last_error": state.last_error,
                # Transitions, not gauges: ``status`` says what is true now,
                # these say how often it changed.
                "opened_total": state.opened_total,
                "half_open_total": state.half_open_total,
                "rejected_total": state.rejected_total,
                "failures_total": state.failures_total,
            }
            for (base_url, model), state in _STATES.items()
        }


def totals() -> dict:
    """The counters summed across providers, for a one-line health readout.

    Summed rather than averaged: "how many calls were refused" is a count of
    events, and averaging it over providers would produce a number that is true
    of nobody.
    """
    with _LOCK:
        keys = ("opened_total", "half_open_total", "rejected_total", "failures_total")
        return {
            key: sum(getattr(state, key) for state in _STATES.values())
            for key in keys
        }
