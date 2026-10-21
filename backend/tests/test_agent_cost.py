"""Cost: priced when the model is known and the meter is complete, else null.

The bug this file guards against is the plausible wrong answer: ``cost_of`` on a
zero-token ``Usage`` returns ``0.0`` when a price is declared. A streamed run
whose provider omitted usage would then render "$0.00" -- a confident lie. The
contract is ``None`` for unpriced and unknown, because zero means free.
"""
from __future__ import annotations

import time

import pytest

from app.agent.loop import AgentRun, TraceStep
from app.agent.service import AgentResult, summarise
from app.config import settings

PRICES = (
    '{"fake-model": {"inputPerMtok": 1.0, "cachedInputPerMtok": 0.25, '
    '"outputPerMtok": 2.0, "currency": "USD"}}'
)


def result(**overrides) -> AgentResult:
    run = AgentRun(goal="price me", model="fake-model")
    run.steps.append(TraceStep(
        round=1, prompt_tokens=1000, cached_prompt_tokens=400, completion_tokens=500,
    ))
    run.prompt_tokens = 1000
    run.cached_prompt_tokens = 400
    run.completion_tokens = 500
    for key, value in overrides.items():
        setattr(run, key, value)
    return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")


@pytest.fixture
def priced(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICES_JSON", PRICES)
    yield
    monkeypatch.setattr(settings, "LLM_PRICES_JSON", "")


def test_a_priced_run_reports_the_arithmetic(priced):
    payload = summarise(result())

    # 600 fresh * 1.0 + 400 cached * 0.25 + 500 out * 2.0 = 1700 / 1e6
    assert payload["model"] == "fake-model"
    assert payload["cost"] == {"amount": 0.0017, "currency": "USD", "estimated": False}
    assert payload["cache_hit_rate"] == 0.4
    assert payload["usage_complete"] is True


def test_an_unpriced_model_is_null_not_zero(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICES_JSON", "")

    payload = summarise(result())

    assert payload["cost"] is None, "an empty price table means unpriced, not free"


def test_an_unreported_meter_is_not_priced_even_with_a_table(priced):
    """A missing usage block makes the total a lower bound, not a bill."""
    payload = summarise(result(usage_complete=False))

    assert payload["cost"] is None


def test_the_cost_uses_the_model_that_ran(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PRICES_JSON", PRICES)
    monkeypatch.setattr(settings, "OPENAI_MODEL", "configured-model")

    payload = summarise(result())

    assert payload["model"] == "fake-model"
    assert payload["cost"] is not None


# --- the snapshot -----------------------------------------------------------


def install(client, monkeypatch, *, model, usage_complete=True):
    import app.routers.agent_runs as routes

    async def fake_execute(goal, *, run_id=None, **kwargs):
        run = AgentRun(
            goal=goal, run_id=run_id, status="succeeded",
            stop_reason="final answer", final_output="done", model=model,
            usage_complete=usage_complete,
            prompt_tokens=1000, cached_prompt_tokens=400, completion_tokens=500,
        )
        step = TraceStep(round=1, prompt_tokens=1000, cached_prompt_tokens=400,
                         completion_tokens=500, usage_missing=not usage_complete)
        run.steps.append(step)
        return AgentResult(run=run, mermaid_sequence="", mermaid_flowchart="", html="")

    monkeypatch.setattr(routes, "is_configured", lambda: True)
    monkeypatch.setattr(routes, "execute", fake_execute)


def wait_for(client, run_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/agent/runs/{run_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


def test_the_detail_endpoint_carries_model_cost_and_usage_state(client, monkeypatch, priced):
    install(client, monkeypatch, model="fake-model")

    run_id = client.post("/api/v1/agent/run", json={"goal": "price it"}).json()["run_id"]
    body = wait_for(client, run_id)

    assert body["model"] == "fake-model"
    assert body["cost"]["amount"] == 0.0017
    assert body["cache_hit_rate"] == 0.4
    assert body["usage_complete"] is True


def test_a_legacy_row_with_no_model_is_not_priced(client, monkeypatch, priced):
    """Rows written before the model column existed have nothing to price."""
    install(client, monkeypatch, model=None)

    run_id = client.post("/api/v1/agent/run", json={"goal": "old row"}).json()["run_id"]
    body = wait_for(client, run_id)

    assert body["model"] is None
    assert body["cost"] is None
