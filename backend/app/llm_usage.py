"""What a model call cost, and who spent it.

Why this module exists
----------------------
Every OpenAI-compatible response carries a ``usage`` object. This codebase read
two of its fields -- ``prompt_tokens`` and ``completion_tokens`` -- and only on
the agent runtime: the eight call sites in ``app.services`` and ``app.workflows``
spent tokens that appeared in no record at all. "What does this system cost to
run" had no answer, and neither did "which feature costs it".

Three things were being thrown away:

1. **Cached input.** DeepSeek reports ``prompt_cache_hit_tokens`` / miss, OpenAI
   reports ``prompt_tokens_details.cached_tokens``. Cached input is billed at a
   fraction of fresh input, so a bare token count cannot be turned into money: a
   call that read 40k cached tokens and one that read 40k fresh ones were
   recorded as the same number and cost very different amounts.
2. **A failed call still cost money.** ``text_of`` raises ``LLMTruncated`` before
   anything reads ``usage``, so the one case where the provider billed for output
   we refuse to use was the case that recorded nothing.
3. **Who spent it.** A token count with no label is a bill with no line items.

Two decisions worth stating
---------------------------
**Prices are configuration, not code.** A hard-coded price is a lie with a shelf
life: it is silently wrong the day the provider changes its table, and it is
wrong in a way nobody notices because the number still looks plausible. So the
arithmetic here is real and the table starts empty -- ``LLM_PRICES_JSON`` supplies
it, and until an operator does, a cost is reported as *unpriced* rather than as
``0``. ``0`` is a claim; ``None`` is the truth.

**A cached token is still a token.** This module reports spend, it does not
pretend a cache hit is free: ``cached_prompt_tokens`` is a subset of
``prompt_tokens``, and only the price table knows that the subset is cheaper.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app.config import settings

logger = logging.getLogger(__name__)

#: Providers disagree on the spelling of "some of your input was a cache hit".
#: Ordered: the first field carrying a non-zero value wins, and the rest are only
#: consulted when it is absent (a provider that reports the DeepSeek spelling
#: does not also report the OpenAI one).
CACHE_READ_FIELDS = (
    "prompt_cache_hit_tokens",      # DeepSeek
    "cached_tokens",                # flat spelling some gateways use
    "cache_read_input_tokens",      # Anthropic-compatible
)

CACHE_MISS_FIELDS = ("prompt_cache_miss_tokens",)   # DeepSeek


def _field(obj: Any, name: str) -> Any:
    """Read one field from a dict, an SDK object, or nothing."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _count(value: Any) -> int:
    """A token count, or zero. Negative and unparseable are both meaningless."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Usage:
    """Token counts for one or more calls, in one canonical shape.

    ``prompt_tokens`` is **all** input tokens, cached ones included; that is the
    invariant every provider is normalised into, because it is the one a reader
    assumes and the one the budget check was already written against.
    """

    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Anthropic-style cache *creation*: input written to the cache, billed above
    #: the fresh rate. Kept apart from a hit because it is not one.
    cache_write_tokens: int = 0

    @property
    def uncached_prompt_tokens(self) -> int:
        return max(0, self.prompt_tokens - self.cached_prompt_tokens)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cache_hit_rate(self) -> Optional[float]:
        """Share of input served from cache, or ``None`` when there was no input.

        ``None`` rather than ``0.0``: a call that sent nothing did not miss the
        cache, it never consulted it.
        """
        if self.prompt_tokens <= 0:
            return None
        return round(self.cached_prompt_tokens / self.prompt_tokens, 4)

    def plus(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            cached_prompt_tokens=self.cached_prompt_tokens + other.cached_prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "cached_prompt_tokens": self.cached_prompt_tokens,
            "uncached_prompt_tokens": self.uncached_prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_rate": self.cache_hit_rate,
        }


def parse_usage(usage: Any) -> Usage:
    """Normalise a provider's ``usage`` object into :class:`Usage`.

    Tolerates three shapes, because this is a seam and the three are all real:

    * DeepSeek: ``prompt_tokens`` + ``prompt_cache_hit_tokens`` / ``miss_tokens``,
      where the two buckets partition the prompt.
    * OpenAI/MiMo: ``prompt_tokens`` + ``prompt_tokens_details.cached_tokens``,
      where the cached count is already part of ``prompt_tokens``.
    * Anthropic-compatible: ``input_tokens`` / ``output_tokens`` with
      ``cache_read_input_tokens`` and ``cache_creation_input_tokens`` billed
      **on top of** ``input_tokens``.

    ``None`` input yields an all-zero :class:`Usage` rather than an exception: a
    provider that omits ``usage`` is a missing measurement, and the caller is
    recording, not deciding.
    """
    if usage is None:
        return Usage()

    anthropic_style = (
        _field(usage, "cache_read_input_tokens") is not None
        and _field(usage, "prompt_tokens") is None
    )

    completion = _count(_field(usage, "completion_tokens")) or _count(
        _field(usage, "output_tokens")
    )

    if anthropic_style:
        cached = _count(_field(usage, "cache_read_input_tokens"))
        written = _count(_field(usage, "cache_creation_input_tokens"))
        base = _count(_field(usage, "input_tokens"))
        return Usage(
            prompt_tokens=base + cached + written,
            cached_prompt_tokens=cached,
            completion_tokens=completion,
            cache_write_tokens=written,
        )

    prompt = _count(_field(usage, "prompt_tokens")) or _count(
        _field(usage, "input_tokens")
    )

    cached = 0
    for name in CACHE_READ_FIELDS:
        cached = _count(_field(usage, name))
        if cached:
            break
    if not cached:
        cached = _count(_field(_field(usage, "prompt_tokens_details"), "cached_tokens"))

    missed = 0
    for name in CACHE_MISS_FIELDS:
        missed = _count(_field(usage, name))
        if missed:
            break

    if not prompt and (cached or missed):
        # Only the two buckets were reported; together they are the prompt.
        prompt = cached + missed
    elif missed and not cached:
        # A miss count without a hit count means the hit count was zero, and the
        # prompt is the miss. Stated arithmetically rather than assumed.
        cached = max(0, prompt - missed)

    if cached > prompt:
        # Inconsistent report. Clamping keeps the invariant true; guessing which
        # number is wrong would be inventing a measurement.
        logger.warning(
            "usage says %s cached of %s prompt tokens; clamping", cached, prompt
        )
        cached = prompt

    return Usage(
        prompt_tokens=prompt,
        cached_prompt_tokens=cached,
        completion_tokens=completion,
    )


@dataclass(frozen=True)
class Price:
    """What one million tokens of each kind costs, as the operator declared it."""

    input_per_mtok: float
    output_per_mtok: float
    #: ``None`` = the operator did not state a separate rate for cache hits, so
    #: hits are costed at the fresh-input rate. That over-estimates, and the
    #: estimate says so (``Cost.estimated``) instead of passing as exact.
    cached_input_per_mtok: Optional[float] = None
    currency: str = "USD"
    source: str = ""


@dataclass(frozen=True)
class Cost:
    amount: float
    currency: str
    #: True when a component had to fall back to a rate that was not stated.
    estimated: bool = False

    def as_dict(self) -> dict:
        return {
            "amount": round(self.amount, 6),
            "currency": self.currency,
            "estimated": self.estimated,
        }


def _load_prices() -> dict:
    """Read ``LLM_PRICES_JSON`` -- a dict of model name to camelCase rates.

    Deliberately re-read each time it is asked for: an operator who fixes a
    price should not have to restart the process, and the cost of parsing a small
    JSON blob is nothing next to a model call.
    """
    raw = (getattr(settings, "LLM_PRICES_JSON", "") or "").strip()
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("LLM_PRICES_JSON is not valid JSON, no prices loaded: %s", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("LLM_PRICES_JSON must be an object, got %s", type(data).__name__)
        return {}

    prices = {}
    for model, spec in data.items():
        if not isinstance(spec, dict):
            continue
        try:
            prices[str(model)] = Price(
                input_per_mtok=float(spec["inputPerMtok"]),
                output_per_mtok=float(spec["outputPerMtok"]),
                cached_input_per_mtok=(
                    None if spec.get("cachedInputPerMtok") is None
                    else float(spec["cachedInputPerMtok"])
                ),
                currency=str(spec.get("currency", "USD")),
                source=str(spec.get("source", "")),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("LLM_PRICES_JSON entry for %r ignored: needs "
                           "inputPerMtok and outputPerMtok", model)
    return prices


def price_for(model: str) -> Optional[Price]:
    """The declared price for ``model``, or ``None`` when nobody declared one.

    Falls back to the longest prefix match so a dated model id
    (``deepseek-chat-0324``) can be covered by one entry (``deepseek-chat``);
    an exact hit always wins.
    """
    prices = _load_prices()
    if not prices or not model:
        return None
    if model in prices:
        return prices[model]

    candidates = [name for name in prices if name and model.startswith(name)]
    if not candidates:
        return None
    return prices[max(candidates, key=len)]


def cost_of(usage: Usage, model: str) -> Optional[Cost]:
    """Money for ``usage`` at ``model``'s declared price, or ``None``.

    ``None`` means "unpriced", not "free" -- see the module docstring. Callers
    are expected to print it as unknown rather than summing it as zero.
    """
    price = price_for(model)
    if price is None:
        return None

    estimated = False
    cached_rate = price.cached_input_per_mtok
    if cached_rate is None:
        cached_rate = price.input_per_mtok
        estimated = usage.cached_prompt_tokens > 0

    # Cache writes have no rate in this table. They are input tokens (billed at
    # a premium the table cannot express), so they are costed at the fresh-input
    # rate and the result is flagged -- under-reported rather than hidden.
    if usage.cache_write_tokens:
        estimated = True

    amount = (
        (usage.uncached_prompt_tokens + usage.cache_write_tokens) * price.input_per_mtok
        + usage.cached_prompt_tokens * cached_rate
        + usage.completion_tokens * price.output_per_mtok
    ) / 1_000_000.0
    return Cost(amount=amount, currency=price.currency, estimated=estimated)


@dataclass
class LedgerEntry:
    """Everything one ``(scope, model)`` pair has spent since process start."""

    scope: str
    model: str
    calls: int = 0
    usage: Usage = field(default_factory=Usage)
    #: Summed only over calls whose model had a declared price.
    cost_amount: float = 0.0
    cost_currency: Optional[str] = None
    priced_calls: int = 0
    estimated_calls: int = 0

    def as_dict(self) -> dict:
        priced = self.priced_calls > 0
        return {
            "scope": self.scope,
            "model": self.model,
            "calls": self.calls,
            "priced_calls": self.priced_calls,
            "estimated_calls": self.estimated_calls,
            **self.usage.as_dict(),
            "cost": (
                {"amount": round(self.cost_amount, 6), "currency": self.cost_currency}
                if priced else None
            ),
        }


_LEDGER: dict = {}
_LOCK = threading.Lock()


def record(scope: str, model: str, usage: Usage) -> LedgerEntry:
    """Add one call's spend under a label.

    ``scope`` is what the spend was *for* -- ``plan``, ``email_draft``,
    ``agent_run`` -- because a total with no breakdown cannot answer "what does
    this feature cost". A caller that does not know should pass ``unlabelled``;
    the point of recording it is that the gap becomes visible.
    """
    key = (scope or "unlabelled", model or "unknown")

    with _LOCK:
        entry = _LEDGER.get(key)
        if entry is None:
            entry = LedgerEntry(scope=key[0], model=key[1])
            _LEDGER[key] = entry
        entry.calls += 1
        entry.usage = entry.usage.plus(usage)

        cost = cost_of(usage, key[1])
        # No price declared: the call is counted as a call and as zero priced
        # calls, which is what ``snapshot`` reads "unpriced" off.
        if cost is not None:
            entry.priced_calls += 1
            if cost.estimated:
                entry.estimated_calls += 1
            entry.cost_amount += cost.amount
            if entry.cost_currency is None:
                entry.cost_currency = cost.currency
            elif entry.cost_currency != cost.currency:
                # Two currencies cannot be added. The entry keeps the first and
                # drops the rest rather than reporting a meaningless sum.
                logger.warning(
                    "scope %r mixed currencies (%s and %s); keeping %s",
                    key[0], entry.cost_currency, cost.currency, entry.cost_currency,
                )
        return entry


def snapshot() -> dict:
    """The ledger, plus totals that say whether they are complete."""
    with _LOCK:
        entries = [e.as_dict() for e in _LEDGER.values()]

    entries.sort(key=lambda e: (e["scope"], e["model"]))

    totals = {
        "calls": sum(e["calls"] for e in entries),
        "prompt_tokens": sum(e["prompt_tokens"] for e in entries),
        "cached_prompt_tokens": sum(e["cached_prompt_tokens"] for e in entries),
        "completion_tokens": sum(e["completion_tokens"] for e in entries),
        "total_tokens": sum(e["total_tokens"] for e in entries),
        "unpriced_calls": sum(
            e["calls"] - e["priced_calls"] for e in entries
        ),
    }
    prompt = totals["prompt_tokens"]
    totals["cache_hit_rate"] = (
        round(totals["cached_prompt_tokens"] / prompt, 4) if prompt else None
    )

    # Only entries that were all priced can be summed into a total; a partial sum
    # presented as "the cost" is the exact mistake this module exists to avoid.
    by_currency: dict = {}
    for entry in entries:
        if entry["cost"] is None or entry["calls"] != entry["priced_calls"]:
            continue
        currency = entry["cost"]["currency"]
        by_currency[currency] = by_currency.get(currency, 0.0) + entry["cost"]["amount"]

    return {
        "entries": entries,
        "totals": totals,
        "cost_totals": {k: round(v, 6) for k, v in by_currency.items()},
        "cost_complete": all(
            e["cost"] is not None and e["calls"] == e["priced_calls"] for e in entries
        ),
        "note": (
            "token counts cover this process only (a restart starts the ledger "
            "again); a null cost means no price was declared for that model, not "
            "that the calls were free"
        ),
    }


def reset() -> None:
    """Clear the ledger. For tests, and for a caller that just wants a delta."""
    with _LOCK:
        _LEDGER.clear()
