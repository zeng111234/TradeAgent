"""Keeping a long run inside the model's context window.

The problem this solves
-----------------------
Every tool observation is appended to the conversation and never leaves it. A
prospect scan that inspects a dozen websites puts ~3,000 characters of scraped
page text into the history *each time*. Measured on the real tools:

    search_prospects        ~1,200 chars per call
    inspect_website         ~3,200 chars per call
    get_customer            ~2,500 chars per call

By round 8 a scan is carrying 25,000+ characters of history. Past the window
the provider either errors or silently truncates, and a silently truncated
history is the worst case: the model loses the evidence it was reasoning from
and nobody is told.

What production harnesses do — and what is done here
---------------------------------------------------
Compaction is deterministic, not a model call. Spending a round trip to
summarise history is itself expensive and can fail, and a summariser that
hallucinates is worse than no summary. Instead:

1. **Recent messages are untouched.** The last few rounds are what the model is
   actively reasoning over.
2. **Old, oversized observations are trimmed**, keeping a head and a tail. The
   head usually carries the beginning of a payload and the tail carries the end
   — for a scraped page that means the company description and the contact
   block.
3. **The agent's own reasoning is never trimmed.** Short messages are left
   alone; the failure mode we are avoiding is losing the thread, not saving
   tokens for their own sake.
4. **Anything dropped is retrievable.** The full text is written to disk and a
   marker names the file. ``recall_observation`` reads it back, so compaction
   loses nothing — it only moves detail out of the active window.

That last point is the design commitment. Compaction that discards information
turns a long run into a quiet failure much later; compaction that moves
information somewhere the agent can ask for it again is safe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Rough character-per-token ratios. English prose runs near 4; JSON and code are
# denser because of punctuation and short keys. Underestimating is the safe
# direction, since the estimate is used to decide when to compact.
CHARS_PER_TOKEN_PROSE = 4.0
CHARS_PER_TOKEN_STRUCTURED = 3.0

# A message shorter than this is never trimmed. It is the model's own reasoning
# or a short tool result, and losing it costs more than the tokens it saves.
MIN_TRIMMABLE_CHARS = 600


def estimate_body_tokens(text: str) -> int:
    """Token estimate for a payload, excluding per-message overhead.

    Exposed so the ratio choice can be tested directly, without per-message
    overhead swamping a small sample.
    """
    if not text:
        return 0
    ratio = CHARS_PER_TOKEN_STRUCTURED if text.lstrip()[:1] in "{[" else CHARS_PER_TOKEN_PROSE
    return int(len(text) / ratio)


def estimate_tokens(messages) -> int:
    """A deliberately conservative token estimate for a message or message list."""
    if messages is None:
        return 0
    if hasattr(messages, "role"):          # a single Message
        messages = [messages]

    total = 0
    for message in messages:
        text = getattr(message, "content", "") or ""
        if text:
            total += estimate_body_tokens(text) + 4      # + per-message overhead
        for call in getattr(message, "tool_calls", None) or []:
            arguments = getattr(call, "arguments", "") or ""
            # Arguments count even when empty: the call itself costs tokens.
            total += int(len(arguments) / CHARS_PER_TOKEN_STRUCTURED) + 12
    return total


def estimate_message_tokens(message) -> int:
    return estimate_tokens(message)


@dataclass
class SpillStore:
    """Keeps the full text of trimmed observations on disk.

    Files are named by content hash, so the same payload written twice is stored
    once and the mapping is stable across runs.
    """

    directory: Path

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, label: str, text: str) -> Path:
        """Store ``text`` and return its path.

        The filename is the content hash alone, so identical payloads from
        different calls resolve to the same file and the name is stable. The
        label is kept inside the file as a provenance header rather than in the
        name, because a label in the name would defeat that deduplication.
        """
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
        path = self.directory / f"{digest}.txt"
        if not path.exists():
            header = f"# {label or 'observation'}\n"
            path.write_text(header + text, encoding="utf-8")
        return path

    def read(self, name: str) -> Optional[str]:
        """Read back a stored observation. Rejects anything outside the store."""
        candidate = (self.directory / name).resolve()
        try:
            candidate.relative_to(self.directory.resolve())
        except ValueError:
            return None                      # path traversal attempt
        if not candidate.is_file():
            return None
        raw = candidate.read_text(encoding="utf-8", errors="replace")
        # Drop the provenance header so the caller gets exactly what was stored.
        if raw.startswith("# "):
            _, _, body = raw.partition("\n")
            return body
        return raw


@dataclass
class CompactionReport:
    """What one compaction pass did. Recorded in the trace, not hidden."""

    dropped_tokens: int = 0
    trimmed_messages: int = 0
    spilled: list[str] = field(default_factory=list)
    # True when the protected messages alone (system prompts + recent exchanges)
    # already exceed the budget, so the request is over budget no matter how much
    # of the middle is trimmed. Reported rather than silently accepted.
    unreachable: bool = False

    @property
    def did_anything(self) -> bool:
        return self.trimmed_messages > 0

    def as_note(self) -> str:
        if not self.did_anything:
            return ""
        parts = [f"compacted {self.trimmed_messages} old observation(s)"]
        if self.dropped_tokens:
            parts.append(f"freed ~{self.dropped_tokens} tokens")
        if self.spilled:
            parts.append(f"{len(self.spilled)} retrievable")
        if self.unreachable:
            parts.append("still over budget: protected messages dominate")
        return ", ".join(parts)


def compact_messages(
    messages: list,
    *,
    budget_tokens: int,
    keep_recent: int = 6,
    spill: Optional[SpillStore] = None,
    head_chars: int = 700,
    tail_chars: int = 400,
    protect_head_messages: int = 0,
) -> tuple[list, CompactionReport]:
    """Return a bounded view of ``messages`` plus a report of what changed.

    The input list is never mutated: the caller keeps the full history for the
    trace and this returns what to actually send.

    Two pieces of arithmetic matter, and getting either wrong makes compaction
    look like it works while the request keeps growing:

    * **Head and tail are protected**, so the trimmable middle is what must
      shrink. If the protected messages alone exceed the budget, no amount of
      trimming reaches it — reported as ``unreachable`` rather than silently
      accepted.
    * **The cut is scaled to the deficit.** A fixed head/tail size frees a fixed
      number of tokens per observation. If each round adds more than compaction
      frees, the request grows without bound while every individual pass reports
      success. So the target for the middle is derived from the budget first,
      and the per-observation cut is sized to hit it.
    """
    from copy import copy

    report = CompactionReport()
    if budget_tokens <= 0 or len(messages) <= keep_recent:
        return list(messages), report

    if estimate_tokens(messages) <= budget_tokens:
        return list(messages), report

    view = list(messages)

    # The system prompts at the front: never trimmed, they define the run.
    protected_head = 0
    while protected_head < len(view) and getattr(view[protected_head], "role", "") == "system":
        protected_head += 1
    # Plus any caller-declared head: the injected conversation history lives
    # here. Trimming it to 80+40 characters would leave the model with "the
    # user asked… the assistant answered…" and no actual content, which is
    # worse than no history at all -- it looks like context and is not.
    if protect_head_messages > 0:
        protected_head = min(len(view), protected_head + protect_head_messages)
    # Plus the most recent exchanges, which the model is reasoning over now.
    protected_tail = max(protected_head, len(view) - keep_recent)

    head_cost = estimate_tokens(view[:protected_head])
    tail_cost = estimate_tokens(view[protected_tail:])
    middle = list(range(protected_head, protected_tail))

    # How much the middle is allowed to cost. Never negative: if the protected
    # messages alone blow the budget, the middle gets nothing.
    target_middle = max(0, budget_tokens - head_cost - tail_cost)
    current_middle = estimate_tokens([view[i] for i in middle])

    if current_middle <= target_middle:
        report.unreachable = head_cost + tail_cost + current_middle > budget_tokens
        return view, report

    # Scale each trimmable observation so the middle lands near its target.
    # Clamped so a large deficit trims hard but never to nothing, and a small
    # deficit stays polite about how much it removes.
    scale = max(0.05, min(0.9, target_middle / current_middle))
    scaled_head = max(80, int(head_chars * scale))
    scaled_tail = max(40, int(tail_chars * scale))

    for index in middle:
        if current_middle <= target_middle:
            break

        message = view[index]
        text = getattr(message, "content", "") or ""
        if len(text) <= MIN_TRIMMABLE_CHARS:
            continue

        replacement = _truncate(text, scaled_head, scaled_tail, spill, index, report)
        if replacement == text:
            continue

        before = estimate_message_tokens(message)
        cloned = copy(message)
        cloned.content = replacement
        view[index] = cloned
        after = estimate_message_tokens(cloned)

        saved = max(0, before - after)
        current_middle -= saved
        report.trimmed_messages += 1
        report.dropped_tokens += saved

    report.unreachable = estimate_tokens(view) > budget_tokens
    return view, report


def _truncate(
    text: str,
    head_chars: int,
    tail_chars: int,
    spill: Optional[SpillStore],
    index: int,
    report: CompactionReport,
) -> str:
    """Keep the head and tail, and say where the rest went."""
    if spill is None:
        marker = f"\n... [{len(text) - head_chars - tail_chars} chars elided by compaction] ...\n"
        return text[:head_chars] + marker + text[-tail_chars:]

    try:
        path = spill.write(f"obs-{index}", text)
    except OSError as exc:                     # disk full, permissions, ...
        logger.warning("could not spill observation %s: %s", index, exc)
        marker = f"\n... [{len(text)} chars elided; not recoverable] ...\n"
        return text[:head_chars] + marker + text[-tail_chars:]

    report.spilled.append(path.name)
    marker = (
        f"\n... [{len(text)} chars elided by compaction. "
        f'Full text retrievable with recall_observation(name="{path.name}")] ...\n'
    )
    return text[:head_chars] + marker + text[-tail_chars:]


def build_recall_tool(spill: SpillStore):
    """A tool that reads back a spilled observation.

    Needs the spill store in a closure, so it is built per run rather than
    declared at module scope like the domain tools.
    """
    from pydantic import BaseModel, Field

    from .tools import tool

    class RecallParams(BaseModel):
        name: str = Field(..., description="The observation name given in the compaction marker")

    class _Omitted(BaseModel):
        pass

    @tool(
        name="recall_observation",
        description=(
            "Read back the full text of an earlier tool observation that was "
            "trimmed to keep the conversation inside the context window. The "
            "compaction marker in the history gives the exact name to pass."
        ),
        params_model=RecallParams,
        tags={"read", "context"},
    )
    async def recall_observation(name: str) -> dict:
        text = spill.read(name)
        if text is None:
            return {
                "error": f"no stored observation named {name!r}. "
                         "Use the exact name from the compaction marker.",
            }
        return {
            "name": name,
            "chars": len(text),
            "text": text,
        }

    return recall_observation
