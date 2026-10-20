"""Shape checks for a live provider.

The test suite has an honest limitation: every fake in it is a shape *we* wrote.
When a provider renames ``usage``, wraps ``content`` in a list, or stops emitting
``tool_calls``, the fakes keep answering and the suite keeps passing. What is
missing is not another test -- it is a scheduled question to the real endpoint,
with an assertion about the shape of the answer.

This module holds the assertions. It is deliberately free of network code so the
checks can be exercised offline; ``scripts/contract_canary.py`` is the thin part
that makes the call and writes the record.

Three things are checked, because three things have silently drifted or would:

1. **Text arrives.** A non-empty ``content`` after ``.strip()``. Wrapping content
   in parts or renaming the field fails here rather than inside a feature.
2. **Usage is countable.** Non-zero prompt and completion tokens. A renamed
   ``usage`` field currently records **0 tokens**, which reads on the ledger as
   "this call was free" instead of "this call was not measured". The canary
   treats zero as drift, not as a cheap day.
3. **A tool call survives the round trip.** A name and JSON-object arguments.
   This is the shape the agent runtime depends on for every action it takes.

The prompt asks for one word on purpose: cheap, fast, and a truncated answer is
itself a signal. The tool check asks for a specific city so a call that arrives
with the wrong name is visible; the arguments are checked for *parseability* and
the expected key, not for semantic correctness -- judging whether the model chose
well is a different question, and not one a shape canary can answer.
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

TEXT_PROMPT = "Reply with exactly one word: ok"

TOOL_NAME = "report_city"
TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Report the city the user is asking about.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
            },
            "required": ["city"],
        },
    },
}
TOOL_PROMPT = "Call report_city with the city Berlin. Do not answer in text."

TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})

OK = "ok"
DRIFT = "drift"
TRANSPORT_ERROR = "transport_error"
SKIPPED = "skipped"


def check_text_reply(reply) -> list[str]:
    """Problems with a plain-text answer. Empty list means the shape held."""
    problems: list[str] = []

    content = getattr(reply, "content", None)
    if not isinstance(content, str) or not content.strip():
        problems.append(
            f"content is not non-empty text (got {type(content).__name__}: "
            f"{content!r}); a provider may have changed the response shape"
        )

    prompt_tokens = getattr(reply, "prompt_tokens", 0) or 0
    if prompt_tokens <= 0:
        problems.append(
            "prompt_tokens is 0 or missing; usage may have been renamed, and the "
            "ledger would record this call as free rather than unmeasured"
        )

    completion_tokens = getattr(reply, "completion_tokens", 0) or 0
    if completion_tokens <= 0:
        problems.append(
            "completion_tokens is 0 or missing; usage may have been renamed"
        )

    finish_reason = getattr(reply, "finish_reason", None)
    if finish_reason in TRUNCATED_FINISH_REASONS:
        problems.append(
            f"finish_reason={finish_reason!r} on a one-word prompt; the token cap "
            "or the provider's accounting has changed"
        )

    return problems


def check_tool_reply(reply) -> list[str]:
    """Problems with a tool-calling answer. Empty list means the shape held."""
    problems: list[str] = []

    calls = getattr(reply, "tool_calls", None) or []
    if not calls:
        problems.append(
            "no tool_calls in the reply; the provider may have stopped emitting "
            "them, or renamed the field"
        )
        return problems

    call = calls[0]
    name = getattr(call, "name", "")
    if not name:
        problems.append("the tool call has an empty name; function.name may have moved")
    elif name != TOOL_NAME:
        problems.append(
            f"the model called {name!r} instead of {TOOL_NAME!r}; the tool schema "
            "may not be reaching the provider"
        )

    raw = getattr(call, "arguments", "")
    try:
        arguments = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except json.JSONDecodeError as exc:
        problems.append(f"tool arguments are not valid JSON: {exc}")
    else:
        if not isinstance(arguments, dict):
            problems.append(
                f"tool arguments decoded to {type(arguments).__name__}, not an object"
            )
        elif "city" not in arguments:
            problems.append("tool arguments are missing the required 'city' key")

    return problems


def verdict(problems: Iterable[str]) -> str:
    """``ok`` when nothing is wrong, ``drift`` otherwise."""
    return DRIFT if list(problems) else OK


def build_record(*, base_url: str, model: str, checks: dict[str, list[str]],
                 checked_at: str, status: Optional[str] = None) -> dict:
    """One line of history. Never contains the credential."""
    all_problems = [problem for problems in checks.values() for problem in problems]
    return {
        "checked_at": checked_at,
        "provider": {"base_url": base_url or "(default)", "model": model or "(default)"},
        "status": status or verdict(all_problems),
        "checks": {
            name: {"status": verdict(problems), "problems": problems}
            for name, problems in checks.items()
        },
        "problems": all_problems,
    }
