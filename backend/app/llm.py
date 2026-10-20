"""The one place the service layer talks to a chat-completions provider.

Why this module exists
----------------------
``app.services.agent_service``, ``app.services.ai_service`` and
``app.workflows`` each built their own ``AsyncOpenAI`` client at every call
site -- ten constructions between them. None of them passed a timeout, so the
SDK default (600 seconds) applied, and none of them looked at
``finish_reason``, so a response cut off at ``max_tokens`` was parsed and
stored as if it were finished.

The agent runtime keeps its own adapter (``app.agent.protocol``) deliberately:
it needs tool-calling, a canonical message format, and an injectable client so
the loop can be driven offline by ``ScriptedLLM``. That adapter does pass a
timeout and does inspect ``finish_reason``. Anything that is *not* the runtime
goes through this module instead.

The exceptions here are the point, not an implementation detail. Callers in
this codebase fall back to a hardcoded template when the model cannot answer,
which is a reasonable product decision -- but the caller has to be able to tell
*why*, so that the fallback can be labelled rather than passed off as model
output. ``LLMUnavailable`` (no key, or a rejected one: the operator can fix it),
``LLMRateLimited`` (come back later), ``LLMTransient`` (the provider failed in a
way that may not repeat), ``LLMTruncated`` (the cap was hit: the answer is
incomplete) and ``LLMBadResponse`` (the provider returned nothing usable) are
different things and are reported as different things. ``LLMCircuitOpen`` is the
sixth: the provider has been failing repeatedly and this call was not attempted,
so its cost is a lock read rather than an 80-second budget.

Why the OpenAI SDK and not a thin httpx wrapper
-----------------------------------------------
Because the part a hand-rolled wrapper would have to reimplement is the part that
is actually hard: retry policy and the classification of a failed request. The
SDK already distinguishes a timeout from a connection error from a rate limit
from an auth rejection from a 5xx, retries the ones worth retrying with backoff,
and pools connections. Wrapping httpx means owning all of that and re-deriving
the taxonomy from status codes -- and it would still be *our* bug when a
provider's edge case shows up.

What the SDK does not do is tell callers in this codebase's vocabulary. That is
this module's job, and until it did that job the seam leaked: every failure
except "no key" arrived at the caller as ``openai.APITimeoutError`` or similar,
so callers wrote ``except Exception`` and flattened five different situations
into one string -- which is exactly what the fallback labelling exists to avoid.
:func:`translate_provider_error` is where that ends.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

from app import circuit
from app.config import settings
from app.llm_usage import parse_usage, record as record_usage
from app.timeouts import LLM_TOTAL_SECONDS

logger = logging.getLogger(__name__)

#: The SDK default is 600s; a ten-minute hang is indistinguishable from a hung
#: server and the browser has given up long before.
DEFAULT_TIMEOUT_SECONDS = 60.0

#: The SDK's own default is 2. Stated here rather than inherited, because
#: "how many times will this retry, and what does it do between attempts" is a
#: decision, and an undeclared default changes under you at upgrade time.
DEFAULT_MAX_RETRIES = 2

#: Providers disagree on the spelling. ``length`` is OpenAI/DeepSeek/MiMo.
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})


class LLMError(RuntimeError):
    """Base class so a caller can catch the whole family in one clause."""


class LLMUnavailable(LLMError):
    """No usable credentials: not configured, or the provider rejected the key.

    Retrying will not help; an operator has to change something.
    """


class LLMRateLimited(LLMError):
    """Too many requests. The call may succeed later, but not immediately.

    ``retry_after`` carries the provider's own answer to "how much later" when it
    gives one (``Retry-After``, or ``x-ratelimit-reset-*`` expressed in seconds).
    It was being thrown away: the exception said "rate limited" and left the
    caller to guess a wait, which is the one number a retry decision actually
    needs. ``None`` means the provider did not say.
    """

    def __init__(self, message: str, *, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMTransient(LLMError):
    """The provider failed in a way that may not repeat.

    A timeout, a dropped connection, a 5xx. The SDK has already retried it
    ``DEFAULT_MAX_RETRIES`` times by the time this is raised, so a caller that
    retries again should mean it.
    """


class LLMTruncated(LLMError):
    """The provider stopped at the token cap, so the answer is incomplete.

    Deliberately an error rather than a warning attached to partial text: half
    a cold email is not an email, and the callers here store what they get as a
    draft a human will send.
    """


class LLMBadResponse(LLMError):
    """The provider returned no choices, no content, or unparseable JSON."""


class LLMCircuitOpen(LLMError):
    """The call was not attempted: the provider has been failing repeatedly.

    Not a provider response and deliberately not a subclass of
    ``LLMTransient``. ``retry.classify`` maps it to **permanent** explicitly:
    retrying a call that was refused before it left the process multiplies
    nothing, and the useful action is to wait for the cooling window. A new
    member of the family rather than a string, so callers that enumerate the
    failure modes (fallback labels, run errors) have to make room for it.
    """

    def __init__(self, message: str, *, retry_at: Optional[datetime] = None,
                 retry_in: Optional[float] = None):
        super().__init__(message)
        self.retry_at = retry_at
        self.retry_in = retry_in


def translate_provider_error(exc: BaseException) -> LLMError:
    """Turn a provider failure into one of this module's exceptions.

    Deliberately duck-typed rather than importing the SDK's exception classes:
    they are renamed and reorganised between releases, and a seam that breaks on
    an upgrade because it enumerated a library's internals is not a seam. What is
    checked is stable across providers -- the module the exception came from, and
    the HTTP status it carries.

    Anything that is **not** the provider's exception is re-raised untouched: a
    ``TypeError`` from our own code must not be reported to a human as "the model
    call failed", which is how a real bug gets hidden behind a friendly fallback.
    """
    origin = (type(exc).__module__ or "").split(".")[0]
    if origin != "openai":
        raise exc

    status = getattr(exc, "status_code", None)
    name = type(exc).__name__

    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return LLMUnavailable(
            f"the provider rejected the credentials ({name}"
            + (f", HTTP {status}" if status else "") + ")"
        )
    if status == 429 or name == "RateLimitError":
        return LLMRateLimited(
            "the provider is rate limiting this key (HTTP 429)",
            retry_after=_retry_after_seconds(exc),
        )
    if status is not None and 500 <= int(status) < 600:
        return LLMTransient(f"the provider returned HTTP {status} ({name})")
    return LLMTransient(f"{name}: {exc}")


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    """The provider's own "wait this long", if it gave one.

    Read off the exception's response headers rather than hard-coded: a 429 with
    ``Retry-After: 20`` means twenty seconds, and a caller that guesses two is
    going to be told no again. Returns ``None`` when the provider said nothing or
    the value is unreadable -- a guess is worse than an honest "unknown".
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None

    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is not None:
            value = float(str(raw).strip())
            return value if value >= 0 else None
    except (TypeError, ValueError):
        return None

    # OpenAI-compatible providers often use these instead, in seconds.
    for header in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        try:
            raw = headers.get(header)
            if raw is not None and str(raw).endswith("s"):
                value = float(str(raw).strip().rstrip("s"))
                return value if value >= 0 else None
        except (AttributeError, TypeError, ValueError):
            continue
    return None


_client: Any = None
_client_signature: Optional[tuple] = None


def timeout_seconds() -> float:
    """Request timeout, overridable by ``OPENAI_TIMEOUT_SECONDS``."""
    configured = getattr(settings, "OPENAI_TIMEOUT_SECONDS", None)
    try:
        value = float(configured)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def max_retries() -> int:
    """How many times the SDK retries a failed request. Overridable."""
    configured = getattr(settings, "OPENAI_MAX_RETRIES", None)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_RETRIES
    return max(0, value)


def is_configured() -> bool:
    """Whether a provider call is even possible."""
    return bool((settings.OPENAI_API_KEY or "").strip())


def get_client():
    """The shared ``AsyncOpenAI``, built once per settings revision.

    Cached because every call site used to construct its own client, which
    meant a fresh connection pool per call. The signature check is what lets
    tests (and a settings reload) get a new client without a process restart.
    """
    global _client, _client_signature

    if not is_configured():
        raise LLMUnavailable("OPENAI_API_KEY is not set")

    signature = (
        settings.OPENAI_API_KEY,
        settings.OPENAI_BASE_URL,
        timeout_seconds(),
        max_retries(),
    )
    if _client is None or _client_signature != signature:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(
            api_key=signature[0],
            base_url=signature[1],
            timeout=signature[2],
            # Stated rather than inherited; see DEFAULT_MAX_RETRIES.
            max_retries=signature[3],
        )
        _client_signature = signature
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests and by config reloads."""
    global _client, _client_signature
    _client = None
    _client_signature = None


def text_of(response: Any) -> str:
    """Extract the assistant text, or raise if there is not a usable one."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise LLMBadResponse("provider returned no choices")

    choice = choices[0]
    reason = getattr(choice, "finish_reason", None) or ""
    if reason in TRUNCATED_FINISH_REASONS:
        raise LLMTruncated(
            f"provider stopped at the token cap (finish_reason={reason!r})"
        )

    content = getattr(getattr(choice, "message", None), "content", None)
    if content is None:
        raise LLMBadResponse("provider returned an empty message")
    if not isinstance(content, str):
        # Some providers answer with a list of content parts. That is a real
        # shape (multimodal replies use it) and, for this application, a
        # contract change: every call site expects text. Name the type, so the
        # drift is readable -- otherwise it surfaces as an AttributeError from
        # our own ``.strip()`` and looks like a bug here.
        raise LLMBadResponse(
            f"provider returned content as {type(content).__name__}, not text; "
            "the response shape may have changed"
        )
    if not content.strip():
        raise LLMBadResponse("provider returned an empty message")

    return content


async def complete_text(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 1500,
    scope: str = "unlabelled",
) -> str:
    """One completion, returned as text. Raises on anything unusable.

    ``scope`` labels the spend in :mod:`app.llm_usage`'s ledger -- ``plan``,
    ``email_draft``, ``reply_analysis``. It is a keyword with a default rather
    than a required argument because the label matters to the *reader* of the
    bill, not to the call: a site that forgets it still spends, and shows up as
    ``unlabelled`` instead of vanishing.
    """
    client = get_client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    key = circuit.provider_key(
        getattr(settings, "OPENAI_BASE_URL", None), settings.OPENAI_MODEL
    )
    # Checked before the request is built, not after: while the circuit is open
    # this raises in microseconds, and the caller labels its fallback with a
    # reason that says "paused", not "timed out".
    circuit.before_call(key)

    try:
        try:
            # ``wait_for`` is not belt-and-braces around the client timeout: the SDK
            # retries *inside* this coroutine, so two 60-second attempts plus backoff
            # are 120+ seconds, while the browser gives up at 120 and this service
            # advertises an 80-second logical call. Without a cap the policy in
            # ``app/timeouts.py`` is a comment, not a budget -- and the extra minute
            # is spent after the user has already seen a timeout.
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.OPENAI_MODEL,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ),
                timeout=LLM_TOTAL_SECONDS,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            # Cancelled by our own budget, not by the provider. It belongs to the
            # transient family: the same call may work later, and the caller's
            # fallback reason must say "ran out of time", not "unexpected error".
            raise LLMTransient(
                f"the model call exceeded its {LLM_TOTAL_SECONDS:.0f}s budget "
                "(attempts and retries included)"
            ) from exc
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - classified, not swallowed
            # The seam's job: a caller here must never have to write
            # `except Exception` to find out that the provider timed out. See
            # translate_provider_error -- anything that is not the provider's own
            # exception is re-raised untouched.
            raise translate_provider_error(exc) from exc
    except LLMError as exc:
        circuit.record_failure(key, exc)
        raise
    except BaseException:
        # Cancellation, or a bug of ours that ``translate_provider_error``
        # deliberately passed through. Neither is evidence about the provider,
        # but the trial slot must be returned -- otherwise one client disconnect
        # during a probe wedges the breaker in half-open.
        circuit.release_trial(key)
        raise

    circuit.record_success(key)

    # Recorded *before* the reply is judged. ``text_of`` raises on a truncated or
    # empty answer, and those are exactly the calls the provider still billed --
    # which made the most expensive failures the ones that recorded nothing.
    record_usage(scope, settings.OPENAI_MODEL, parse_usage(getattr(response, "usage", None)))

    return text_of(response)


def strip_code_fence(text: str) -> str:
    """Undo the ```` ```json ... ``` ```` wrapper models add unprompted.

    This logic was copied at seven call sites. They all used
    ``text.split("```")[1]``, which keeps whatever followed the closing fence,
    so a model that answered with JSON plus a sentence of commentary produced a
    parse error rather than an answer.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    # Drop the opening fence and its optional language tag.
    body = stripped[3:]
    if body[:4].lower() == "json":
        body = body[4:]
    elif body[:1] == "\n":
        body = body[1:]

    # Cut at the closing fence, if there is one, so trailing prose is dropped.
    end = body.find("```")
    if end != -1:
        body = body[:end]
    return body.strip()


def parse_json_object(text: str) -> Any:
    """Parse a model's JSON answer, tolerating a code fence."""
    cleaned = strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMBadResponse(f"provider did not return valid JSON: {exc}") from exc


async def complete_json(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 1500,
    scope: str = "unlabelled",
) -> Any:
    """One completion, parsed as JSON. Raises on anything unusable."""
    return parse_json_object(
        await complete_text(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            scope=scope,
        )
    )
