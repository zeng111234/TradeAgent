"""The provider seam: why the SDK, and what the seam owes its callers.

**The question this file answers.** Why ``AsyncOpenAI`` instead of a thin httpx
wrapper? Because the part a hand-rolled wrapper would have to reimplement is the
part that is actually hard, and the SDK already does it: it tells a timeout from
a connection error from a rate limit from an auth rejection from a 5xx, retries
the ones worth retrying with backoff, and pools connections. Wrapping httpx means
owning all of that and re-deriving the taxonomy from status codes -- and it would
still be our bug when a provider edge case appears.

**What the seam owed and did not deliver.** The SDK speaks in `openai.*`
exceptions. Callers here speak in fallback reasons. Until this round, every
failure except "no key" reached the caller as `openai.APITimeoutError` or its
siblings, so callers wrote `except Exception` and flattened five distinct
situations into one string:

    except Exception as exc:                      # network, auth, rate limit
        reason = f"model call failed: {exc}"

That comment is the defect written down. A rate limit ("come back shortly"), a
dead provider ("may not repeat"), a rejected key ("an operator must fix this")
and no key at all are not the same event, and the whole degradation story of this
service is about saying *which* one happened.
"""
from __future__ import annotations

import pytest

from app import llm
from app.llm import (
    LLMBadResponse,
    LLMError,
    LLMRateLimited,
    LLMTransient,
    LLMTruncated,
    LLMUnavailable,
    translate_provider_error,
    complete_text,
    parse_json_object,
    strip_code_fence,
)


def fake_openai_error(name: str, status=None):
    """An exception that looks like the SDK's, without importing a fake module.

    The seam is deliberately duck-typed -- it checks the exception's module and
    its status, not its class identity -- precisely so that an SDK release that
    renames or reorganises its exceptions does not break it. This builds the same
    shape to test that.
    """
    error_type = type(name, (Exception,), {"__module__": "openai"})
    error = error_type(f"simulated {name}")
    if status is not None:
        error.status_code = status
    return error


# --------------------------------------------------------------------------
# The taxonomy
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error,expected",
    [
        (fake_openai_error("APITimeoutError"), LLMTransient),
        (fake_openai_error("APIConnectionError"), LLMTransient),
        (fake_openai_error("InternalServerError", 500), LLMTransient),
        (fake_openai_error("BadGatewayError", 502), LLMTransient),
        (fake_openai_error("RateLimitError", 429), LLMRateLimited),
        (fake_openai_error("AuthenticationError", 401), LLMUnavailable),
        (fake_openai_error("PermissionDeniedError", 403), LLMUnavailable),
    ],
)
def test_provider_failures_become_this_module_s_own_types(error, expected):
    assert isinstance(translate_provider_error(error), expected)


def test_a_rejected_key_is_reported_like_a_missing_one():
    """"No key" and "the key is wrong" need the same person to act.

    Both are operator problems, neither is retryable, and a caller deciding
    between "fall back and tell the user" and "retry later" treats them the same.
    """
    rejected = translate_provider_error(fake_openai_error("AuthenticationError", 401))
    assert isinstance(rejected, LLMUnavailable)
    assert "credentials" in str(rejected)


@pytest.mark.parametrize("status", [400, 404, 422])
def test_a_client_error_that_is_not_auth_is_still_not_swallowed(status):
    """A 4xx we caused is transient as far as this seam is concerned, but visible.

    It is reported rather than dropped on the floor: the message carries the
    status, so the log says 400 rather than "the model call failed".
    """
    translated = translate_provider_error(fake_openai_error("BadRequestError", status))
    assert isinstance(translated, LLMTransient)
    assert str(status) in str(translated) or "BadRequest" in str(translated)


# --------------------------------------------------------------------------
# What must NOT be translated
# --------------------------------------------------------------------------

def test_our_own_bug_is_not_reported_as_a_provider_failure():
    """The reason this is duck-typed rather than `except Exception: raise ours`.

    A TypeError from our own code reaching a human as "the model call failed" is
    how a real defect hides behind a friendly fallback for a year.
    """
    with pytest.raises(TypeError):
        translate_provider_error(TypeError("our own mistake"))


def test_a_non_provider_exception_survives_the_wrapper(monkeypatch):
    """Same property, through the real entry point."""
    class Exploding:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    raise ValueError("not the provider's problem")

    monkeypatch.setattr(llm, "get_client", lambda: Exploding)

    import asyncio

    with pytest.raises(ValueError, match="not the provider's problem"):
        asyncio.run(complete_text("hello"))


def test_a_provider_exception_is_translated_through_the_real_entry_point(monkeypatch):
    class Exploding:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    raise fake_openai_error("APITimeoutError")

    monkeypatch.setattr(llm, "get_client", lambda: Exploding)

    import asyncio

    with pytest.raises(LLMTransient):
        asyncio.run(complete_text("hello"))


def test_every_translated_error_is_in_the_family():
    """So a caller can still catch the whole thing in one clause."""
    for error in (
        fake_openai_error("APITimeoutError"),
        fake_openai_error("RateLimitError", 429),
        fake_openai_error("AuthenticationError", 401),
    ):
        assert isinstance(translate_provider_error(error), LLMError)


# --------------------------------------------------------------------------
# The settings the SDK would otherwise decide silently
# --------------------------------------------------------------------------

def test_the_retry_policy_is_stated_not_inherited():
    """The SDK's default is 2. An undeclared default is a decision nobody made.

    "How many times will this retry, and what happens in between" changes what a
    degraded call looks like to a user: a timeout that is retried twice takes
    three times as long to surface.
    """
    assert llm.DEFAULT_MAX_RETRIES == 2
    assert llm.max_retries() == llm.DEFAULT_MAX_RETRIES

    class Settings:
        OPENAI_TIMEOUT_SECONDS = None
        OPENAI_MAX_RETRIES = "5"

    original = llm.settings
    llm.settings = Settings()
    try:
        assert llm.max_retries() == 5
    finally:
        llm.settings = original


def test_an_unusable_retry_setting_falls_back_instead_of_crashing():
    class Settings:
        OPENAI_TIMEOUT_SECONDS = None
        OPENAI_MAX_RETRIES = "not a number"

    original = llm.settings
    llm.settings = Settings()
    try:
        assert llm.max_retries() == llm.DEFAULT_MAX_RETRIES
    finally:
        llm.settings = original


def test_the_client_is_built_with_the_timeout_and_the_retries(monkeypatch):
    """The two numbers are only real if they reach the constructor."""
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import sys
    import types

    fake_module = types.ModuleType("openai")
    fake_module.AsyncOpenAI = FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_module)

    class Settings:
        OPENAI_API_KEY = "test-key"
        OPENAI_BASE_URL = "https://example.test/v1"
        OPENAI_MODEL = "test-model"
        OPENAI_TIMEOUT_SECONDS = 30
        OPENAI_MAX_RETRIES = 4

    monkeypatch.setattr(llm, "settings", Settings())
    llm.reset_client()
    try:
        llm.get_client()
    finally:
        llm.reset_client()

    assert captured["timeout"] == 30
    assert captured["max_retries"] == 4, (
        "the retry count never reached the client; the SDK default would apply"
    )
    assert captured["base_url"] == "https://example.test/v1"


# --------------------------------------------------------------------------
# The response shape the SDK parses for us
# --------------------------------------------------------------------------

class Choice:
    def __init__(self, finish_reason="stop", content="hello"):
        self.finish_reason = finish_reason
        self.message = type("M", (), {"content": content})()


class Response:
    def __init__(self, *choices):
        self.choices = list(choices)


def test_a_truncated_answer_is_an_error_not_partial_text():
    for reason in ("length", "max_tokens"):
        with pytest.raises(LLMTruncated):
            llm.text_of(Response(Choice(finish_reason=reason)))


def test_a_fenced_json_answer_is_parsed():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_trailing_prose_after_a_fence_is_dropped():
    """The bug the seven copied `split("```")[1]` versions had."""
    assert strip_code_fence('```json\n{"a": 1}\n```\nHope that helps!') == '{"a": 1}'


def test_unparseable_json_is_a_bad_response_not_a_crash():
    with pytest.raises(LLMBadResponse):
        parse_json_object("I am afraid I cannot do that")


def test_no_choices_is_a_bad_response():
    with pytest.raises(LLMBadResponse):
        llm.text_of(Response())


def test_an_empty_message_is_a_bad_response():
    with pytest.raises(LLMBadResponse):
        llm.text_of(Response(Choice(content="   ")))


def test_no_key_raises_unavailable_rather_than_calling_anything(monkeypatch):
    class Settings:
        OPENAI_API_KEY = ""
        OPENAI_BASE_URL = None
        OPENAI_TIMEOUT_SECONDS = None
        OPENAI_MAX_RETRIES = None

    monkeypatch.setattr(llm, "settings", Settings())
    llm.reset_client()
    try:
        with pytest.raises(LLMUnavailable):
            llm.get_client()
    finally:
        llm.reset_client()


# --------------------------------------------------------------------------
# The caller's side of the contract
# --------------------------------------------------------------------------

def test_the_agent_runtime_speaks_the_same_vocabulary(monkeypatch):
    """Two provider seams, one language.

    ``app/agent/protocol.py`` is the runtime's own adapter -- it needs tool
    calling and an injectable client, so it does not go through ``app/llm.py``
    for the call itself. It does go through the same *translator*, because a
    run's ``error`` field is read by a human and by whatever decides whether to
    retry: "LLMRateLimited" means one thing, "APITimeoutError" means the reader
    has to know the SDK.
    """
    import asyncio

    from app.agent.protocol import OpenAICompatibleClient

    client = OpenAICompatibleClient(
        api_key="test-key", base_url="https://example.test/v1", model="test-model",
    )

    class Exploding:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    raise fake_openai_error("RateLimitError", 429)

    monkeypatch.setattr(client, "_ensure_client", lambda: Exploding)

    with pytest.raises(LLMRateLimited):
        asyncio.run(client.chat([], []))


def test_a_run_records_a_translated_reason():
    """And the loop turns it into a run error in that same vocabulary."""
    import asyncio

    from app.agent.loop import AgentBudget, AgentLoop
    from app.agent.loop import AgentError
    from app.agent.tools import ToolRegistry

    class Exploding:
        async def chat(self, *_args, **_kwargs):
            raise LLMTransient("the provider returned HTTP 503")

    agent_loop = AgentLoop(Exploding(), ToolRegistry(), budget=AgentBudget(max_rounds=2))

    with pytest.raises(AgentError) as caught:
        asyncio.run(agent_loop.run("do something"))

    assert "LLMTransient" in str(caught.value), (
        f"the run's error did not name the class of failure: {caught.value}"
    )


def test_each_failure_reaches_the_caller_as_a_distinct_reason(monkeypatch):
    """The whole point of the taxonomy, asserted where a human reads it.

    ``generate_cold_email`` falls back to a template. The fallback is fine; the
    excuse must not be one string for four situations.

    Note the shape of the flow, which the first version of this test got wrong:
    "no key" is decided *before* the call, by ``is_configured()``. The other four
    can only arrive as exceptions from the seam -- so the seam has to be told a
    key exists for them to be reachable at all.
    """
    import asyncio

    from app.services import ai_service

    async def call():
        return await ai_service.generate_cold_email(
            product_name="gold thread",
            target_industry="textiles",
            target_country="Germany",
            company_name="Us",
        )

    # 1. Not configured: decided before any call is made.
    monkeypatch.setattr(ai_service, "is_configured", lambda: False)
    unconfigured = asyncio.run(call())
    assert unconfigured.get("fallback_reason") == "no_api_key"

    # 2. Configured, and the provider fails in each of the ways it can.
    monkeypatch.setattr(ai_service, "is_configured", lambda: True)

    reasons = {}
    for name, error in (
        ("no_key", LLMUnavailable("no key")),
        ("rate_limited", LLMRateLimited("429")),
        ("transient", LLMTransient("timeout")),
        ("truncated", LLMTruncated("length")),
        ("bad_response", LLMBadResponse("empty")),
    ):
        async def boom(*_args, _error=error, **_kwargs):
            raise _error

        monkeypatch.setattr(ai_service, "complete_text", boom)
        result = asyncio.run(call())
        reasons[name] = result.get("fallback_reason") or result.get("ai_powered")

    assert len(set(map(str, reasons.values()))) == len(reasons), (
        "two different failures produced the same explanation: " + repr(reasons)
    )
    assert all(reason for reason in reasons.values()), reasons


def test_a_bug_in_our_own_code_is_not_explained_as_the_model_failing(monkeypatch):
    """Same property as the seam's, asserted one layer up where a human reads it.

    If a TypeError from our own code produced "model call failed", a real defect
    would hide behind the template fallback indefinitely.
    """
    import asyncio

    from app.services import ai_service

    async def boom(*_args, **_kwargs):
        raise TypeError("our own mistake")

    monkeypatch.setattr(ai_service, "is_configured", lambda: True)
    monkeypatch.setattr(ai_service, "complete_text", boom)

    result = asyncio.run(ai_service.generate_cold_email(
        product_name="gold thread", company_name="Us",
    ))

    reason = result.get("fallback_reason", "")
    assert "unexpected" in reason and "TypeError" in reason, (
        f"our own bug was reported as a model failure: {reason!r}"
    )
