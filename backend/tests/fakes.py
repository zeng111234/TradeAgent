"""Test doubles.

``FakeLLM`` is the reason the agent loop can be tested without a network: the
loop only depends on the ``LLMClient`` protocol, so a scripted reply list is
enough to drive any branch of the state machine deterministically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union

from app.agent.protocol import LLMReply, Message, ToolCall


@dataclass
class ChatCall:
    """One recorded ``chat()`` invocation."""

    messages: list[Message]
    tools: list[dict]
    temperature: float
    max_tokens: int


@dataclass
class FakeLLM:
    """Returns pre-scripted replies in order and records what it was asked.

    A script entry may be:

    * a ``str``  -> assistant text, no tool calls
    * a ``dict`` -> ``{"content": ..., "tool_calls": [{"name","arguments","id"}]}``
    * an ``LLMReply`` -> used verbatim
    * an ``Exception`` -> raised, to exercise error handling
    """

    script: list[Union[str, dict, LLMReply, Exception]]
    calls: list[ChatCall] = field(default_factory=list)
    default_tokens: int = 10
    _index: int = 0

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict],
        *,
        temperature: float = 0.2,
        max_tokens: int = 1500,
    ) -> LLMReply:
        # Copy so later mutation of the loop's message list cannot rewrite history.
        self.calls.append(ChatCall(list(messages), list(tools), temperature, max_tokens))

        if self._index >= len(self.script):
            raise AssertionError(
                f"FakeLLM ran out of scripted replies after {self._index} call(s). "
                "Add another entry — this usually means the loop is not terminating."
            )

        entry = self.script[self._index]
        self._index += 1

        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, LLMReply):
            return entry
        if isinstance(entry, str):
            return LLMReply(
                content=entry,
                prompt_tokens=self.default_tokens,
                completion_tokens=self.default_tokens,
            )

        # dict form
        tool_calls = [
            ToolCall(
                id=tc.get("id") or f"call_{i}",
                name=tc.get("name", ""),
                arguments=tc.get("arguments", "{}")
                if isinstance(tc.get("arguments", "{}"), str)
                else __import__("json").dumps(tc["arguments"]),
            )
            for i, tc in enumerate(entry.get("tool_calls") or [])
        ]
        return LLMReply(
            content=entry.get("content", ""),
            tool_calls=tool_calls,
            prompt_tokens=entry.get("prompt_tokens", self.default_tokens),
            completion_tokens=entry.get("completion_tokens", self.default_tokens),
            finish_reason=entry.get("finish_reason"),
        )

    # -- helpers for assertions -------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def tools_offered(self) -> Optional[list[dict]]:
        return self.calls[0].tools if self.calls else None

    def offered_tool_names(self) -> list[str]:
        if not self.calls:
            return []
        return sorted(t["function"]["name"] for t in self.calls[0].tools)

    def last_messages(self) -> list[Message]:
        return self.calls[-1].messages if self.calls else []


def tool_reply(name: str, arguments: Any = None, *, content: str = "", call_id: str = "") -> dict:
    """Build a one-tool-call script entry."""
    import json

    return {
        "content": content,
        "tool_calls": [
            {
                "id": call_id or f"call_{name}",
                "name": name,
                "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments or {}),
            }
        ],
    }


def multi_tool_reply(*calls: tuple[str, Any], content: str = "") -> dict:
    """Build a script entry requesting several tools in one round."""
    import json

    return {
        "content": content,
        "tool_calls": [
            {
                "id": f"call_{i}_{name}",
                "name": name,
                "arguments": args if isinstance(args, str) else json.dumps(args or {}),
            }
            for i, (name, args) in enumerate(calls)
        ],
    }


def final(text: str = "done") -> str:
    """A concluding text answer long enough to count as substantive."""
    return text if len(text) >= 200 else text + " " + ("x" * (200 - len(text)))
