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
output. ``LLMUnavailable`` (no key: the operator can fix it),
``LLMTruncated`` (the cap was hit: the answer is incomplete) and
``LLMBadResponse`` (the provider returned nothing usable) are three different
things and are reported as three different things.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

#: The SDK default is 600s; a ten-minute hang is indistinguishable from a hung
#: server and the browser has given up long before.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Providers disagree on the spelling. ``length`` is OpenAI/DeepSeek/MiMo.
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})


class LLMError(RuntimeError):
    """Base class so a caller can catch the whole family in one clause."""


class LLMUnavailable(LLMError):
    """No API key is configured. Retrying will not help."""


class LLMTruncated(LLMError):
    """The provider stopped at the token cap, so the answer is incomplete.

    Deliberately an error rather than a warning attached to partial text: half
    a cold email is not an email, and the callers here store what they get as a
    draft a human will send.
    """


class LLMBadResponse(LLMError):
    """The provider returned no choices, no content, or unparseable JSON."""


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
    )
    if _client is None or _client_signature != signature:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(
            api_key=signature[0],
            base_url=signature[1],
            timeout=signature[2],
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
    if content is None or not content.strip():
        raise LLMBadResponse("provider returned an empty message")

    return content


async def complete_text(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 1500,
) -> str:
    """One completion, returned as text. Raises on anything unusable."""
    client = get_client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = await client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
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
) -> Any:
    """One completion, parsed as JSON. Raises on anything unusable."""
    return parse_json_object(
        await complete_text(
            prompt,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    )
