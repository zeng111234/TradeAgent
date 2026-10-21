"""Wire format for the agent loop.

The loop stores one canonical message shape so it does not depend on any
vendor's SDK. A thin adapter converts to and from the OpenAI chat-completions
format, which is what every OpenAI-compatible endpoint speaks.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


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
    #: The model's reasoning channel, when the provider exposes one. Kept apart
    #: from ``content`` on purpose: ``content`` is what the assistant said, this
    #: is what it thought before saying it. A run that shows only ``content``
    #: looks like a model that never thinks -- which is what the console used to
    #: show, because this field did not exist and the provider's
    #: ``reasoning_content`` was dropped on the floor.
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: How much of ``prompt_tokens`` the provider served from its prefix cache.
    #: A subset, not an addition: see ``app.llm_usage`` for why the split is the
    #: difference between knowing what a run cost and only knowing how big it was.
    cached_prompt_tokens: int = 0
    #: False when the provider returned no usage block at all. Zero tokens and
    #: "the provider did not say" are different statements, and conflating them
    #: turns a missing meter into a run that appears to have been free.
    usage_reported: bool = True
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


def reasoning_of(message: Any) -> str:
    """Read the provider's reasoning channel, if it has one.

    Three spellings are in the wild for the same idea: DeepSeek and MiMo put it
    in ``reasoning_content``, OpenRouter-style routers use ``reasoning``, and
    some gateways nest it under ``model_extra``. Anthropic-compatible endpoints
    return content as a list of parts instead, which the caller extracts
    separately.

    Read-only by design: providers either reject a transcript that echoes
    reasoning back, or require a signature this runtime cannot mint. So this
    text is recorded in the trace and never replayed to the model.
    """
    for name in ("reasoning_content", "reasoning"):
        value = getattr(message, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    extra = getattr(message, "model_extra", None)
    if isinstance(extra, dict):
        for name in ("reasoning_content", "reasoning"):
            value = extra.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _content_parts(raw: list) -> tuple[str, str]:
    """Split a list-shaped content into visible text and thinking text.

    Anthropic and several gateways send ``[{"type": "text", ...},
    {"type": "thinking", ...}]`` where OpenAI sends a string. Returning both
    halves keeps the trace honest; returning neither is a shape this runtime
    cannot read, and the caller raises with the shape named.
    """
    texts: list[str] = []
    thinking: list[str] = []
    for part in raw:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind in ("thinking", "reasoning"):
            value = part.get("thinking") or part.get("text") or part.get("reasoning")
            if isinstance(value, str):
                thinking.append(value)
    return "\n".join(texts).strip(), "\n".join(thinking).strip()


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


class _StreamUnavailable(Exception):
    """Streaming produced nothing usable before a single delta was delivered.

    Internal signal, not an error for the caller: it means "answer this round
    without ``stream=True``". Raised only while nothing has been shown to a
    user, so the retry cannot duplicate output or bill a second answer.

    ``kind`` matters because the three causes call for different policies:

    * ``capability`` -- the endpoint answered 4xx to a streaming request. It
      will answer the same way next round, so stop asking for the run.
    * ``transport`` -- a dropped connection, a timeout, a 429, a 5xx. Weather.
      Disabling streaming for the rest of the run because one socket closed is
      how a long run silently loses its typewriter.
    * ``empty`` -- accepted, then closed without a chunk. Usually a buffering
      gateway; possibly a hiccup. Tolerated once, then treated as capability.
    """

    def __init__(self, kind: str = "transport") -> None:
        super().__init__(kind)
        self.kind = kind
        self.permanent = kind == "capability"


def _rejects_stream_options(exc: Exception) -> bool:
    """True when the failure is specifically about the usage option.

    Some OpenAI-compatible gateways validate ``stream_options`` instead of
    ignoring it. Dropping the field keeps streaming; retrying the whole call as
    non-streaming would give up a feature for a field the caller can live
    without.
    """
    text = str(exc).lower()
    return "stream_options" in text or "include_usage" in text


def _stream_refusal_kind(exc: Exception) -> str:
    """Classify a failed streaming request: capability or weather.

    Only an HTTP client error that is not a timeout, a conflict or throttling
    says something durable about the endpoint. Everything else -- connection
    reset, DNS, proxy, 5xx -- says something about this moment, and the next
    round deserves its own attempt.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 429):
        return "capability"
    return "transport"


class OpenAICompatibleClient:
    """Adapter over ``openai.AsyncOpenAI`` (works for MiMo / DeepSeek / OpenAI)."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        *,
        stream: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.model = model
        #: Read by the loop before it passes ``on_delta``. A fake client does
        #: not define it, which is what keeps every scripted test unchanged.
        self.supports_stream = bool(stream)
        #: Cleared when a provider rejects ``stream_options``; kept per client
        #: instance because a run creates one client.
        self._include_usage = True
        #: Consecutive accepted-but-empty streams. One is a hiccup, two is a
        #: gateway that does not really stream.
        self._empty_streams = 0
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
        on_delta: Optional[Callable[[str, str], None]] = None,
    ) -> LLMReply:
        """One model call, streamed when a delta sink is supplied.

        ``on_delta(kind, text)`` is a **per-call** argument, not an attribute on
        the client: a sub-agent shares its parent's client instance, and client
        state would push the child's tokens into the parent's transcript.
        """
        client = self._ensure_client()
        kwargs = self._request_kwargs(
            messages, tools, temperature=temperature, max_tokens=max_tokens
        )

        # Same breaker as ``app.llm``: same key, same state. An outage seen by
        # one seam is knowledge the other seam needs, or the runtime would keep
        # burning 80-second budgets while the legacy path fails fast.
        from app import circuit

        key = circuit.provider_key(self._base_url, self.model)
        circuit.before_call(key)

        if on_delta is not None and self.supports_stream:
            try:
                return await self._chat_stream(client, kwargs, on_delta, key)
            except _StreamUnavailable as exc:
                # Nothing reached a user, so a non-streaming retry cannot
                # duplicate output. How long streaming stays off depends on
                # whose fault the failure was; see _StreamUnavailable.
                cause = type(exc.__cause__).__name__ if exc.__cause__ else "no chunk"
                if exc.permanent:
                    self.supports_stream = False
                    logger.warning(
                        "provider %s does not stream (%s); answering without "
                        "deltas for the rest of this run",
                        self._base_url, cause,
                    )
                elif exc.kind == "empty":
                    self._empty_streams += 1
                    if self._empty_streams >= 2:
                        self.supports_stream = False
                        logger.warning(
                            "provider %s closed two streams with no chunks (%s); "
                            "answering without deltas for the rest of this run",
                            self._base_url, cause,
                        )
                    else:
                        logger.warning(
                            "provider %s streamed no chunks before closing (%s); "
                            "answering this round without deltas",
                            self._base_url, cause,
                        )
                else:
                    logger.warning(
                        "streaming failed before the first chunk (%s); answering "
                        "this round without deltas, next round will retry",
                        cause,
                    )

        return await self._chat_once(client, kwargs, key)

    def _request_kwargs(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def _record_failure(self, key: str, exc: Exception) -> None:
        """Translate a provider failure into this codebase's vocabulary.

        Always raises. Kept in one place so the streaming and non-streaming
        paths cannot drift into reporting the same outage two different ways.
        """
        from app import circuit
        from app.llm import LLMError, translate_provider_error

        if isinstance(exc, LLMError):
            # Already in our vocabulary (a scripted client, or an adapter
            # layered above this one). It is provider evidence, not our bug.
            circuit.record_failure(key, exc)
            raise exc

        try:
            translated = translate_provider_error(exc)
        except BaseException:
            # Not the provider's exception: our own bug. Return the trial
            # without judging availability.
            circuit.release_trial(key)
            raise
        circuit.record_failure(key, translated)
        raise translated from exc

    async def _chat_once(
        self, client, kwargs: dict[str, Any], key: str
    ) -> LLMReply:
        from app import circuit

        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - translated, not swallowed
            # One translator for both seams; see ``_record_failure``. The class
            # name in the message is the whole value of the translation: it is
            # how a run's ``stop_reason`` distinguishes "come back later" from
            # "an operator has to fix this".
            self._record_failure(key, exc)

        circuit.record_success(key)

        if not response.choices:
            return LLMReply(content="", finish_reason="empty_choices")

        choice = response.choices[0]
        message = choice.message
        raw_content = message.content
        parts_reasoning = ""
        if isinstance(raw_content, list):
            # A list of content parts is a valid provider shape. Before this,
            # it was rejected outright, which threw away the thinking blocks
            # along with the text. Split it; only fail if neither is there.
            text, parts_reasoning = _content_parts(raw_content)
            if not text and not parts_reasoning:
                from app.llm import LLMBadResponse

                raise LLMBadResponse(
                    "provider returned a list of content parts with no text or "
                    "thinking block; the response shape may have changed"
                )
            raw_content = text
        elif raw_content is not None and not isinstance(raw_content, str):
            # Same contract change as in ``app.llm.text_of``: a shape this
            # runtime cannot read fails with the shape named, not with
            # AttributeError.
            from app.llm import LLMBadResponse

            raise LLMBadResponse(
                f"provider returned content as {type(raw_content).__name__}, not "
                "text; the response shape may have changed"
            )
        reply = LLMReply(
            content=(raw_content or "").strip(),
            reasoning=(reasoning_of(message) or parts_reasoning).strip(),
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
        if usage is None:
            # Not "zero": the provider simply did not report. The distinction
            # matters downstream -- a zero would be priced as a free run.
            reply.usage_reported = False
        else:
            # One parser for three provider spellings; see app.llm_usage.
            from app.llm_usage import parse_usage

            parsed = parse_usage(usage)
            reply.prompt_tokens = parsed.prompt_tokens
            reply.cached_prompt_tokens = parsed.cached_prompt_tokens
            reply.completion_tokens = parsed.completion_tokens

        return reply

    async def _chat_stream(
        self,
        client,
        kwargs: dict[str, Any],
        on_delta: Callable[[str, str], None],
        key: str,
    ) -> LLMReply:
        """Consume a provider stream, forwarding deltas as they arrive.

        The accumulated reply is identical in shape to the non-streaming one, so
        the loop cannot tell the difference except for the deltas it saw.
        """
        from app import circuit
        from app.llm_usage import parse_usage

        stream_kwargs = dict(kwargs, stream=True)
        if self._include_usage:
            stream_kwargs["stream_options"] = {"include_usage": True}

        try:
            stream = await client.chat.completions.create(**stream_kwargs)
        except Exception as exc:
            if self._include_usage and _rejects_stream_options(exc):
                # The gateway validates the field instead of ignoring it. Drop
                # the field and keep the stream: usage is nice, deltas are the
                # feature the caller asked for.
                self._include_usage = False
                stream_kwargs.pop("stream_options", None)
                try:
                    stream = await client.chat.completions.create(**stream_kwargs)
                except Exception as retry:  # noqa: BLE001 - reported by caller
                    raise _StreamUnavailable(_stream_refusal_kind(retry)) from retry
            else:
                raise _StreamUnavailable(_stream_refusal_kind(exc)) from exc

        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason = None
        usage = None
        received = False

        try:
            async for chunk in stream:
                if not received:
                    received = True
                    self._empty_streams = 0
                    # Only now is the provider demonstrably serving. Recording
                    # success when ``create()`` returned would mark the breaker
                    # healthy before a single byte crossed the wire.
                    circuit.record_success(key)

                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage

                for choice in (getattr(chunk, "choices", None) or []):
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        text = getattr(delta, "content", None)
                        if isinstance(text, str) and text:
                            content.append(text)
                            on_delta("content", text)

                        thinking = getattr(delta, "reasoning_content", None)
                        if not isinstance(thinking, str) or not thinking:
                            thinking = getattr(delta, "reasoning", None)
                        if isinstance(thinking, str) and thinking:
                            reasoning.append(thinking)
                            on_delta("reasoning", thinking)

                        # Tool calls arrive as fragments: the id and name in one
                        # chunk, the JSON arguments split across many. They are
                        # merged by index, which is the only field guaranteed
                        # stable across the fragments.
                        for raw in (getattr(delta, "tool_calls", None) or []):
                            index = getattr(raw, "index", 0)
                            slot = calls.setdefault(
                                index if isinstance(index, int) else 0,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            raw_id = getattr(raw, "id", None)
                            if raw_id:
                                slot["id"] = raw_id
                            fn = getattr(raw, "function", None)
                            if fn is not None:
                                name = getattr(fn, "name", None)
                                if name:
                                    slot["name"] = name
                                arguments = getattr(fn, "arguments", None)
                                if arguments:
                                    slot["arguments"] += arguments

                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
        except Exception as exc:  # noqa: BLE001 - translated, not swallowed
            if not received:
                raise _StreamUnavailable("transport") from exc
            # Output already reached the user. Re-sending would duplicate it and
            # bill twice; this is a failure, not a retry. The marker is what
            # tells the loop's retry policy not to try again.
            try:
                self._record_failure(key, exc)
            except Exception as translated:  # noqa: BLE001 - re-raised as itself
                translated.delivered = True
                raise
        finally:
            # An async iterator abandoned mid-flight holds an HTTP connection.
            # Without this, a cancelled or timed-out round leaks a socket.
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - must not mask the result
                    logger.debug("could not close a model stream", exc_info=True)

        if not received:
            # An accepted request that produced no chunk at all. Nothing was
            # delivered, so the non-streaming retry is safe.
            raise _StreamUnavailable("empty")

        reply = LLMReply(
            content="".join(content).strip(),
            reasoning="".join(reasoning).strip(),
            finish_reason=finish_reason,
        )
        for index in sorted(calls):
            slot = calls[index]
            if not slot["name"]:
                continue
            reply.tool_calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=slot["arguments"],
                )
            )

        if usage is None:
            reply.usage_reported = False
        else:
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
