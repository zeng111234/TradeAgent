"""Wire format for the agent loop.

The loop stores one canonical message shape so it does not depend on any
vendor's SDK. A thin adapter converts to and from the OpenAI chat-completions
format, which is what every OpenAI-compatible endpoint speaks.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class ToolCall:
    """One function call requested by the model."""

    id: str
    name: str
    # Raw string as returned by the provider — NOT pre-parsed. Real endpoints
    # do emit malformed JSON here, and recovering from that is a feature.
    arguments: str = ""


@dataclass
class LLMReply:
    """Canonical model reply."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: How much of ``prompt_tokens`` the provider served from its prefix cache.
    #: A subset, not an addition: see ``app.llm_usage`` for why the split is the
    #: difference between knowing what a run cost and only knowing how big it was.
    cached_prompt_tokens: int = 0
    finish_reason: Optional[str] = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Message:
    """Canonical conversation message."""

    role: str                       # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    @staticmethod
    def system(text: str) -> "Message":
        return Message(role="system", content=text)

    @staticmethod
    def user(text: str) -> "Message":
        return Message(role="user", content=text)

    @staticmethod
    def assistant(text: str = "", tool_calls: Optional[list[ToolCall]] = None) -> "Message":
        return Message(role="assistant", content=text or "", tool_calls=list(tool_calls or []))

    @staticmethod
    def tool_result(call: ToolCall, observation: str) -> "Message":
        return Message(
            role="tool",
            content=observation,
            tool_call_id=call.id,
            name=call.name,
        )

    def to_openai(self) -> dict:
        if self.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": self.tool_call_id,
                "content": self.content,
            }
        if self.role == "assistant" and self.tool_calls:
            return {
                "role": "assistant",
                # Providers reject assistant messages that carry neither
                # content nor tool_calls, and some reject empty-string content
                # alongside tool_calls — send None in that case.
                "content": self.content or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments or "{}"},
                    }
                    for c in self.tool_calls
                ],
            }
        return {"role": self.role, "content": self.content}


@runtime_checkable
class LLMClient(Protocol):
    """What the loop needs from a model.

    Both the real OpenAI-compatible client and the test fake satisfy this,
    which is what makes the loop testable without a network.
    """

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1500,
    ) -> LLMReply:  # pragma: no cover - protocol
        ...


class OpenAICompatibleClient:
    """Adapter over ``openai.AsyncOpenAI`` (works for MiMo / DeepSeek / OpenAI)."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.model = model
        self._api_key = api_key
        self._base_url = base_url
        self._timeout = timeout
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1500,
    ) -> LLMReply:
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # Same breaker as ``app.llm``: same key, same state. An outage seen by
        # one seam is knowledge the other seam needs, or the runtime would keep
        # burning 80-second budgets while the legacy path fails fast.
        from app import circuit

        key = circuit.provider_key(self._base_url, self.model)
        circuit.before_call(key)

        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - translated, not swallowed
            # The other provider seam in this codebase, and it owed callers the
            # same thing: the OpenAI SDK speaks in `openai.*` exceptions, and the
            # loop above records `stop_reason`/`error` for a human to read. Both
            # seams go through one translator so the vocabulary is the same --
            # `LLMRateLimited` in a run's error means the same thing as in a
            # fallback reason, and "retry this run later" can be decided from it
            # instead of from an exception's class name.
            from app.llm import LLMError, translate_provider_error

            if isinstance(exc, LLMError):
                # Already in our vocabulary (a scripted client, or an adapter
                # layered above this one). It is provider evidence, not our bug.
                circuit.record_failure(key, exc)
                raise

            try:
                translated = translate_provider_error(exc)
            except BaseException:
                # Not the provider's exception: our own bug. Return the trial
                # without judging availability.
                circuit.release_trial(key)
                raise
            circuit.record_failure(key, translated)
            raise translated from exc

        circuit.record_success(key)

        if not response.choices:
            return LLMReply(content="", finish_reason="empty_choices")

        choice = response.choices[0]
        message = choice.message
        raw_content = message.content
        if raw_content is not None and not isinstance(raw_content, str):
            # Same contract change as in ``app.llm.text_of``: a list of content
            # parts is a valid provider shape and not one this runtime can
            # read. Fail with the shape named, not with AttributeError.
            from app.llm import LLMBadResponse

            raise LLMBadResponse(
                f"provider returned content as {type(raw_content).__name__}, not "
                "text; the response shape may have changed"
            )
        reply = LLMReply(
            content=(raw_content or "").strip(),
            finish_reason=getattr(choice, "finish_reason", None),
        )

        for raw in (getattr(message, "tool_calls", None) or []):
            fn = getattr(raw, "function", None)
            reply.tool_calls.append(
                ToolCall(
                    id=getattr(raw, "id", "") or f"call_{len(reply.tool_calls)}",
                    name=getattr(fn, "name", "") if fn else "",
                    arguments=getattr(fn, "arguments", "") if fn else "",
                )
            )

        usage = getattr(response, "usage", None)
        if usage is not None:
            # One parser for three provider spellings; see app.llm_usage.
            from app.llm_usage import parse_usage

            parsed = parse_usage(usage)
            reply.prompt_tokens = parsed.prompt_tokens
            reply.cached_prompt_tokens = parsed.cached_prompt_tokens
            reply.completion_tokens = parsed.completion_tokens

        return reply


def parse_arguments(raw: str) -> dict:
    """Best-effort decode, for display only. The loop passes raw strings through."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
