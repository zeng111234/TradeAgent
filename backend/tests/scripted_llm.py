"""Reusable scripted LLM for scan tests.

The real loop, the real registry and the real budget checks all run; only the
model is replaced. That keeps these tests offline and deterministic while still
exercising the code path production uses.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from app.agent.protocol import LLMReply, ToolCall
from app.agent.tools import Tool, ToolRegistry


@dataclass
class ChatCall:
    """One recorded ``chat()`` invocation.

    Kept as a record rather than a bare message list so a test can assert on what
    the model was *offered* (the tool schemas) as well as what it was told.
    """

    messages: list
    tools: list
    temperature: float = 0.2
    max_tokens: int = 1500

    # Convenience: iteration and len behave like the message list, so simple
    # assertions such as ``child_llm.calls[0][-1].content`` keep working.
    def __iter__(self):
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index):
        return self.messages[index]


@dataclass
class ScriptedLLM:
    """Returns pre-scripted replies in order and records what it was asked."""

    script: list[dict]
    calls: list[ChatCall] = field(default_factory=list)
    prompt_tokens: int = 150
    completion_tokens: int = 80
    _index: int = 0

    async def chat(self, messages, tools, *, temperature=0.2, max_tokens=1500):
        self.calls.append(ChatCall(list(messages), list(tools), temperature, max_tokens))
        if self._index >= len(self.script):
            return LLMReply(content="(script exhausted)")
        entry = self.script[self._index]
        self._index += 1
        return LLMReply(
            content=entry.get("content", ""),
            tool_calls=[
                ToolCall(
                    id=tc.get("id", f"call_{i}"),
                    name=tc["name"],
                    arguments=tc["arguments"]
                    if isinstance(tc["arguments"], str)
                    else json.dumps(tc["arguments"]),
                )
                for i, tc in enumerate(entry.get("tool_calls", []))
            ],
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
        )

    # -- helpers for assertions -------------------------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def offered_tool_names(self) -> list[str]:
        if not self.calls:
            return []
        return sorted(entry["function"]["name"] for entry in self.calls[0].tools)

    def last_messages(self) -> list:
        return self.calls[-1].messages if self.calls else []


def call(name: str, arguments: Optional[dict] = None, *, content: str = "") -> dict:
    return {"content": content, "tool_calls": [{"name": name, "arguments": arguments or {}}]}


def final(text: str) -> dict:
    """A concluding text answer, long enough to count as substantive."""
    body = text if len(text) >= 200 else text + " " + ("." * (200 - len(text)))
    return {"content": body}


# --- stubbing the network tools -------------------------------------------

CANNED_SEARCH = {
    "count": 2,
    "queries_run": ["embroidery thread importer Germany"],
    "candidates": [
        {"company_name": "Example Textil GmbH", "website": "https://example-textil.example",
         "domain": "example-textil.example", "snippet": "Importer of embroidery threads",
         "search_keyword": "embroidery thread", "search_country": "Germany"},
        {"company_name": "Nordstickerei Handel", "website": "https://nordstickerei.example",
         "domain": "nordstickerei.example", "snippet": "Wholesale needlework supplies",
         "search_keyword": "embroidery thread", "search_country": "Germany"},
    ],
    "errors": [],
    "note": "Unranked and unscored by design.",
}

CANNED_PAGE = {
    "url": "https://example-textil.example",
    "status_code": 200,
    "title": "Example Textil GmbH",
    "emails": ["einkauf@example-textil.example"],
    "phones": ["+49 40 123456"],
    "text": "Example Textil GmbH imports embroidery threads and metallic yarns.",
    "text_truncated": False,
    "text_total_chars": 70,
}


def registry_with_stubs(registry: ToolRegistry, stubs: dict[str, Any]) -> ToolRegistry:
    """Copy a registry, swapping the bodies of the named tools.

    Names, descriptions and parameter schemas come from the real tools, so the
    model is driven by exactly the interface production exposes.
    """
    replaced = ToolRegistry()
    for name in registry.names():
        original = registry.get(name)
        if name in stubs:
            replaced.register(
                Tool(name, original.description, original.params_model,
                     stubs[name], set(original.tags))
            )
        else:
            replaced.register(original)
    return replaced


def scan_stubs(*, search=None, inspect=None) -> dict:
    async def stubbed_search(keyword: str, country: str = "", max_results: int = 8) -> dict:
        return search or CANNED_SEARCH

    async def stubbed_inspect(url: str, max_chars: int = 3000) -> dict:
        page = dict(inspect or CANNED_PAGE)
        page["url"] = url
        return page

    return {"search_prospects": stubbed_search, "inspect_website": stubbed_inspect}
