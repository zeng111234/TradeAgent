"""Token spend: what was measured, what was thrown away, what a cost is.

The through-line of every test here is the same distinction: a token count is a
measurement, a price is a declaration, and ``None`` is not ``0``. The code that
recorded two integers per run could not tell a cheap call from an expensive one,
could not see eight of its ten call sites at all, and recorded nothing for the
one case the provider definitely billed -- a truncated reply.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import llm, llm_usage
from app.config import settings
from app.llm_usage import (
    Usage,
    cost_of,
    parse_usage,
    record,
    reset,
    snapshot,
)


@pytest.fixture(autouse=True)
def clean_ledger():
    reset()
    yield
    reset()


def prices(monkeypatch, table: dict) -> None:
    monkeypatch.setattr(settings, "LLM_PRICES_JSON", json.dumps(table))


# --------------------------------------------------------------------------
# parse_usage: three provider spellings, one shape
# --------------------------------------------------------------------------

def test_no_usage_is_a_missing_measurement_not_an_error():
    assert parse_usage(None) == Usage()


def test_deepseek_spelling_partitions_the_prompt():
    usage = parse_usage(
        SimpleNamespace(
            prompt_tokens=1000,
            prompt_cache_hit_tokens=800,
            prompt_cache_miss_tokens=200,
            completion_tokens=50,
        )
    )
    assert usage.prompt_tokens == 1000
    assert usage.cached_prompt_tokens == 800
    assert usage.uncached_prompt_tokens == 200
    assert usage.completion_tokens == 50
    assert usage.cache_hit_rate == 0.8


def test_openai_nested_details_are_read():
    usage = parse_usage({
        "prompt_tokens": 400,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 300},
    })
    assert (usage.prompt_tokens, usage.cached_prompt_tokens) == (400, 300)


def test_anthropic_spelling_bills_cache_reads_on_top_of_input():
    usage = parse_usage({
        "input_tokens": 100,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 50,
        "output_tokens": 20,
    })
    # 100 + 900 + 50: the Anthropic fields are disjoint, unlike OpenAI's, and a
    # parser that assumed one shape would understate the prompt by 10x here.
    assert usage.prompt_tokens == 1050
    assert usage.cached_prompt_tokens == 900
    assert usage.cache_write_tokens == 50
    assert usage.completion_tokens == 20


def test_only_the_two_buckets_reported_still_yields_a_prompt():
    usage = parse_usage({"prompt_cache_hit_tokens": 640, "prompt_cache_miss_tokens": 64})
    assert usage.prompt_tokens == 704
    assert usage.cached_prompt_tokens == 640


def test_a_hit_count_larger_than_the_prompt_is_clamped_not_trusted():
    usage = parse_usage({"prompt_tokens": 100, "prompt_cache_hit_tokens": 500})
    assert usage.cached_prompt_tokens == 100, "the cached share cannot exceed the input"


def test_a_reply_with_no_prompt_tokens_has_no_hit_rate():
    assert Usage(completion_tokens=12).cache_hit_rate is None, (
        "0.0 would claim the cache was consulted and missed; it was never consulted"
    )


# --------------------------------------------------------------------------
# cost: the arithmetic is real, the prices are declared
# --------------------------------------------------------------------------

def test_an_undeclared_model_is_unpriced_not_free(monkeypatch):
    prices(monkeypatch, {})
    assert cost_of(Usage(1_000_000, 0, 1_000_000), "some-model") is None


def test_cached_input_is_charged_at_its_own_rate(monkeypatch):
    prices(monkeypatch, {"m": {"inputPerMtok": 1.0, "cachedInputPerMtok": 0.1,
                               "outputPerMtok": 2.0}})
    cost = cost_of(Usage(prompt_tokens=1_000_000, cached_prompt_tokens=600_000,
                         completion_tokens=500_000), "m")
    # 400k fresh * 1.0 + 600k cached * 0.1 + 500k out * 2.0, per million.
    assert cost.amount == pytest.approx(0.4 + 0.06 + 1.0)
    assert cost.estimated is False


def test_no_cached_rate_means_an_estimate_that_says_so(monkeypatch):
    prices(monkeypatch, {"m": {"inputPerMtok": 1.0, "outputPerMtok": 2.0}})
    cost = cost_of(Usage(prompt_tokens=1_000_000, cached_prompt_tokens=600_000), "m")
    assert cost.amount == pytest.approx(1.0), "hits fall back to the fresh rate"
    assert cost.estimated is True, "an unstated rate must not pass as exact"


def test_a_dated_model_id_is_covered_by_its_prefix(monkeypatch):
    prices(monkeypatch, {
        "deepseek-chat": {"inputPerMtok": 1.0, "outputPerMtok": 2.0},
        "deepseek-chat-0324": {"inputPerMtok": 5.0, "outputPerMtok": 5.0},
    })
    assert llm_usage.price_for("deepseek-chat-0324").input_per_mtok == 5.0, (
        "an exact entry must beat the prefix"
    )
    assert llm_usage.price_for("deepseek-chat-1111").input_per_mtok == 1.0


def test_broken_price_configuration_prices_nothing_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICES_JSON", "{not json")
    assert llm_usage.price_for("anything") is None


# --------------------------------------------------------------------------
# the ledger: who spent it
# --------------------------------------------------------------------------

def test_spend_is_attributed_to_the_scope_that_incurred_it(monkeypatch):
    prices(monkeypatch, {})
    record("plan", "m", Usage(prompt_tokens=100, completion_tokens=10))
    record("email_draft", "m", Usage(prompt_tokens=50, completion_tokens=40))
    record("email_draft", "m", Usage(prompt_tokens=50, completion_tokens=40))

    snap = snapshot()
    by_scope = {e["scope"]: e for e in snap["entries"]}

    assert by_scope["plan"]["calls"] == 1
    assert by_scope["email_draft"]["calls"] == 2
    assert by_scope["email_draft"]["completion_tokens"] == 80
    assert snap["totals"]["calls"] == 3
    assert snap["totals"]["prompt_tokens"] == 200


def test_an_unpriced_scope_makes_the_total_incomplete_rather_than_smaller(monkeypatch):
    prices(monkeypatch, {"priced": {"inputPerMtok": 1.0, "outputPerMtok": 1.0}})
    record("a", "priced", Usage(prompt_tokens=1_000_000))
    record("b", "unpriced-model", Usage(prompt_tokens=1_000_000))

    snap = snapshot()
    assert snap["cost_totals"] == {"USD": pytest.approx(1.0)}, (
        "the priced half sums; the other half is not silently counted as zero"
    )
    assert snap["cost_complete"] is False
    assert snap["totals"]["unpriced_calls"] == 1


def test_a_fully_priced_ledger_says_its_total_is_complete(monkeypatch):
    prices(monkeypatch, {"m": {"inputPerMtok": 2.0, "outputPerMtok": 0.0}})
    record("a", "m", Usage(prompt_tokens=500_000))
    record("b", "m", Usage(prompt_tokens=500_000))

    snap = snapshot()
    assert snap["cost_complete"] is True
    assert snap["cost_totals"]["USD"] == pytest.approx(2.0)


# --------------------------------------------------------------------------
# through the seam: the two fields that used to be invisible
# --------------------------------------------------------------------------

class _StubClient:
    """Stands in for ``AsyncOpenAI``: records the request, returns a canned body."""

    def __init__(self, outcome):
        self.calls: list[dict] = []
        self._outcome = outcome
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _response(text="hi", *, finish_reason="stop", usage=None):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


def test_complete_text_records_the_cache_split_under_its_scope(monkeypatch):
    stub = _StubClient(_response("ok", usage=SimpleNamespace(
        prompt_tokens=1000, prompt_cache_hit_tokens=900, completion_tokens=25,
    )))
    monkeypatch.setattr(llm, "get_client", lambda: stub)

    asyncio.run(llm.complete_text("p", scope="plan"))

    entry = snapshot()["entries"][0]
    assert entry["scope"] == "plan"
    assert (entry["prompt_tokens"], entry["cached_prompt_tokens"]) == (1000, 900)


def test_a_truncated_reply_is_recorded_because_it_was_billed(monkeypatch):
    """The failure case is the expensive case, and it used to record nothing.

    ``text_of`` raises on ``finish_reason=length`` -- and until the recording
    moved above that check, the one call guaranteed to have burned output tokens
    for an answer we refuse to use left no trace at all.
    """
    stub = _StubClient(_response("half an em", finish_reason="length",
                                 usage=SimpleNamespace(prompt_tokens=700,
                                                       completion_tokens=400)))
    monkeypatch.setattr(llm, "get_client", lambda: stub)

    with pytest.raises(llm.LLMTruncated):
        asyncio.run(llm.complete_text("p", scope="email_draft"))

    snap = snapshot()
    assert snap["totals"]["calls"] == 1
    assert snap["totals"]["completion_tokens"] == 400


def test_a_provider_failure_records_no_spend_because_none_was_measured(monkeypatch):
    stub = _StubClient(RuntimeError("boom"))
    monkeypatch.setattr(llm, "get_client", lambda: stub)

    # Not an LLMError: an exception from our own code is re-raised untouched by
    # the seam, and it is certainly not a bill.
    with pytest.raises(RuntimeError):
        asyncio.run(llm.complete_text("p", scope="plan"))

    assert snapshot()["totals"]["calls"] == 0, (
        "a request that never returned has no usage to record; inventing one "
        "would be a made-up measurement"
    )


def test_the_agent_adapter_parses_the_cache_fields_off_a_real_response(monkeypatch):
    from app.agent.protocol import OpenAICompatibleClient

    client = OpenAICompatibleClient(api_key="k", base_url="http://x", model="m")
    monkeypatch.setattr(client, "_ensure_client", lambda: _StubClient(
        _response("done", usage=SimpleNamespace(
            prompt_tokens=5000,
            prompt_tokens_details=SimpleNamespace(cached_tokens=4096),
            completion_tokens=100,
        ))
    ))

    reply = asyncio.run(client.chat([], []))
    assert reply.prompt_tokens == 5000
    assert reply.cached_prompt_tokens == 4096
    assert reply.completion_tokens == 100


# --------------------------------------------------------------------------
# the run carries the split, and the loop reports it
# --------------------------------------------------------------------------

def test_a_run_records_its_cache_hits_per_round_and_in_total():
    from app.agent import AgentBudget, AgentLoop, ToolRegistry
    from tests.fakes import FakeLLM, final

    llm_client = FakeLLM(script=[
        {"content": "ok", "prompt_tokens": 900, "cached_prompt_tokens": 800,
         "completion_tokens": 10},
        {"content": final("done"), "prompt_tokens": 1500, "cached_prompt_tokens": 1400,
         "completion_tokens": 20},
    ])

    run = asyncio.run(
        AgentLoop(llm_client, ToolRegistry(), budget=AgentBudget(max_rounds=3)).run("go")
    )

    assert [s.cached_prompt_tokens for s in run.steps] == [800, 1400]
    assert run.cached_prompt_tokens == 2200
    assert run.prompt_tokens == 2400
    assert run.as_dict()["steps"][0]["cached_prompt_tokens"] == 800
    assert run.as_dict()["cached_prompt_tokens"] == 2200


def test_run_spend_reaches_the_ledger_under_the_agent_scope():
    from app.agent import AgentBudget, AgentLoop, ToolRegistry
    from tests.fakes import FakeLLM, final

    llm_client = FakeLLM(script=[{"content": final("done"), "prompt_tokens": 400,
                                  "cached_prompt_tokens": 300, "completion_tokens": 60}])
    asyncio.run(AgentLoop(llm_client, ToolRegistry(), budget=AgentBudget(max_rounds=2)).run("go"))

    entry = snapshot()["entries"][0]
    assert entry["scope"] == "agent_run"
    assert entry["cached_prompt_tokens"] == 300


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------

def test_the_usage_route_reports_the_ledger():
    from fastapi.testclient import TestClient
    from app.main import app

    record("plan", "unpriced-model", Usage(prompt_tokens=10, completion_tokens=1))

    with TestClient(app) as c:
        body = c.get("/api/v1/usage").json()

    assert body["totals"]["calls"] == 1
    assert body["entries"][0]["cost"] is None
    assert body["cost_complete"] is False
