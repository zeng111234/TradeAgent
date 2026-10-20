"""What to retry, and how long to wait before doing it again.

Retrying is not a setting, it is a judgement about *why* something failed, and it
is only safe when the operation can be repeated. Getting it wrong in either
direction has a cost:

* retrying the wrong thing -- a rejected key, a malformed request, our own bug --
  multiplies a failure that will never succeed, and turns "the credentials are
  wrong" into "the service is slow". The SDK already retried it twice.
* not retrying the right thing -- a dropped connection, a 5xx, a rate limit --
  loses work that a single second would have saved.

There is also a third category that is neither: failures that *can* be tried again
but not with the same inputs. A truncated answer retried with the same token cap
is a guaranteed repeat; a JSON parse failure retried at the same temperature is
the same coin flip. Those need a **repair** -- a different request -- not a retry.

Where the retries already are, and what this module adds:

* the model SDK retries connection errors, 408/409/429/5xx with its own backoff
  (``DEFAULT_MAX_RETRIES``). Above it, a second blind retry layer would multiply:
  three attempts times two layers is six requests for one logical call.
* nothing else retried at all. A page fetch that hit a transient reset was simply
  a page the agent never read.

So the policy here is used where the SDK is not: it decides, once, and it is the
only place that knows the difference between retryable, repairable and permanent.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: The three answers. ``REPAIR`` is deliberately not called retryable: it means
#: "try again **with different inputs**, or it will fail the same way".
RETRY = "retry"
REPAIR = "repair"
PERMANENT = "permanent"


@dataclass(frozen=True)
class Decision:
    """What to do about one failure."""

    kind: str
    wait_seconds: float
    reason: str

    @property
    def retryable(self) -> bool:
        return self.kind in (RETRY, REPAIR)


#: Backoff shape. Full jitter (uniform between 0 and the ceiling) rather than
#: fixed or exponential: when ten workers are rate-limited at the same instant, a
#: deterministic backoff has them all return at the same instant too.
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0
#: Providers occasionally send a ``Retry-After`` measured in hours. Waiting that
#: long inside a request is not a retry, it is a hang -- so anything beyond this
#: is treated as permanent for this call.
MAX_HONOURED_RETRY_AFTER_SECONDS = 60.0


def backoff_seconds(attempt: int, *, base: float = BASE_DELAY_SECONDS,
                    cap: float = MAX_DELAY_SECONDS, jitter: bool = True) -> float:
    """How long to wait before attempt number ``attempt`` (1-based).

    Exponential ceiling with full jitter. Capped, because an uncapped exponential
    eventually exceeds every budget in ``app/timeouts.py`` and the retry becomes
    a slower way of failing.
    """
    attempt = max(1, int(attempt))
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0, ceiling) if jitter else ceiling


def classify(exc: BaseException) -> Decision:
    """Decide about one failure. The whole retry policy is this function.

    Order matters: the most specific answer wins, and anything unrecognised is
    **permanent**. That default is the important one -- an unknown exception is
    usually our own bug, and retrying a bug is how it turns into a timeout.
    """
    from app.llm import (
        LLMBadResponse,
        LLMCircuitOpen,
        LLMRateLimited,
        LLMTransient,
        LLMTruncated,
        LLMUnavailable,
    )

    if isinstance(exc, LLMCircuitOpen):
        # Explicit, not left to the default branch. The default is permanent
        # today, but "retry a call the breaker refused" is exactly the sort of
        # semantic that would survive a future change to that default, silently.
        return Decision(PERMANENT, 0.0,
                        "the provider circuit is open; the call was not attempted")

    if isinstance(exc, LLMRateLimited):
        given = getattr(exc, "retry_after", None)
        if given is not None and 0 < given <= MAX_HONOURED_RETRY_AFTER_SECONDS:
            # The provider said how long. Our own guess is strictly worse.
            return Decision(RETRY, float(given),
                            f"rate limited; the provider asked for {given:.0f}s")
        if given is not None:
            return Decision(PERMANENT, 0.0,
                            f"rate limited for {given:.0f}s, which is longer than a "
                            "request can wait; not retrying here")
        return Decision(RETRY, backoff_seconds(1),
                        "rate limited; the provider did not say for how long")

    if isinstance(exc, LLMTransient):
        # The SDK already tried DEFAULT_MAX_RETRIES times before this surfaced.
        return Decision(RETRY, backoff_seconds(1),
                        "transient provider failure (the SDK's own retries are spent)")

    if isinstance(exc, LLMTruncated):
        # Not retryable with the same inputs: same prompt, same cap, same cut.
        return Decision(REPAIR, 0.0,
                        "the answer was cut off at the token cap; a repeat with the "
                        "same cap would be cut off again")

    if isinstance(exc, LLMBadResponse):
        # A parse failure is a coin flip: a second sample can come back well-formed.
        return Decision(REPAIR, 0.0,
                        "the provider returned something unusable; one fresh sample "
                        "may be usable")

    if isinstance(exc, LLMUnavailable):
        # No key, or a rejected one. Both need a person, and neither improves by
        # trying again.
        return Decision(PERMANENT, 0.0, "credentials or configuration, not a transient fault")

    return Decision(PERMANENT, 0.0,
                    f"{type(exc).__name__} is not a failure this policy knows how to "
                    "retry; treating it as our own bug")


def should_retry(exc: BaseException) -> bool:
    """Convenience for callers that only need the yes/no."""
    return classify(exc).retryable


def run_with_retries(
    operation: Callable[[], object],
    *,
    attempts: int = 2,
    deadline: Optional[float] = None,
    what: str = "operation",
    retry_only: Optional[tuple[type, ...]] = None,
):
    """Call ``operation`` up to ``attempts`` times, honouring the policy.

    Synchronous on purpose: the places in this codebase that lacked retries are
    blocking calls running on a worker thread (a page fetch), and an ``async``
    helper would have to be awaited inside a coroutine that is deliberately not
    one.

    ``deadline`` is a ``time.monotonic()`` value. Waiting past it is not a retry,
    it is a slower way of failing -- see ``app/timeouts.py`` for why the inner
    layer must stay inside the outer one.

    ``retry_only`` narrows what may be retried at all: an operation whose failures
    are mostly permanent (an SMTP send) should say so rather than inherit a
    policy written for something else.
    """
    last: Optional[BaseException] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - classified below
            last = exc
            if retry_only is not None and not isinstance(exc, retry_only):
                raise
            decision = classify(exc)
            if not decision.retryable or attempt >= attempts:
                logger.warning("%s failed on attempt %d/%d: %s (%s)", what, attempt,
                               attempts, exc, decision.reason)
                raise
            wait = decision.wait_seconds
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= wait:
                    logger.warning(
                        "%s: not retrying %s (needs %.1fs, %.1fs left in the budget)",
                        what, type(exc).__name__, wait, max(0.0, remaining),
                    )
                    raise
            logger.info("%s: attempt %d/%d failed (%s); retrying in %.1fs",
                        what, attempt, attempts, decision.reason, wait)
            time.sleep(wait)
    raise last  # pragma: no cover - the loop either returns or raises


def classify_http_status(status: int) -> Decision:
    """For blocking HTTP clients (``requests``) that have no exception taxonomy.

    The same three answers, decided from the only signal available:
    408/429/5xx are worth another try; everything else in the 4xx range is the
    request's fault and will fail identically.
    """
    if status == 429:
        return Decision(RETRY, backoff_seconds(1), "HTTP 429")
    if status in (408, 425):
        return Decision(RETRY, backoff_seconds(1), f"HTTP {status}")
    if 500 <= status < 600:
        return Decision(RETRY, backoff_seconds(1), f"HTTP {status}")
    if 400 <= status < 500:
        return Decision(PERMANENT, 0.0,
                        f"HTTP {status}: the request is wrong, not the moment")
    return Decision(PERMANENT, 0.0, f"HTTP {status}")
