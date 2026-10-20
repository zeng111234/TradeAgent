"""Diagnostics: the three numbers that decide whether a request is served.

The circuit breaker, the run slots and the connection pool all refuse work, and
none of them could be read without a debugger. A stuck circuit was the worst of
the three: every caller sees the same "provider unavailable", so "it opened once
a minute ago" and "it has opened eleven times this minute" look identical from
outside.

Two properties are asserted here beyond the shape of the response:

* **The counters follow the state machine.** ``opened_total`` counts
  transitions, not seconds spent open; ``half_open_total`` counts trials
  granted, not trials intended.
* **Reading changes nothing.** A diagnostics call during an incident must not
  claim the probe, reset a cooldown, or hold a pooled connection.
"""
from __future__ import annotations

import pytest

from app import circuit
from app.llm import LLMTransient

KEY = ("https://provider.test", "diagnostics-model")
KEY_LABEL = "https://provider.test|diagnostics-model"


@pytest.fixture(autouse=True)
def _clean_circuits():
    circuit.reset()
    yield
    circuit.reset()


def _fail(times: int, at: float) -> None:
    for _ in range(times):
        circuit.record_failure(KEY, LLMTransient("the provider returned HTTP 503"), now=at)


def test_the_endpoint_reports_the_three_places_work_is_refused(client):
    body = client.get("/api/v1/diagnostics").json()

    assert set(body) >= {"circuits", "runs", "database", "generated_at"}
    assert body["runs"]["limit"] >= 1
    assert isinstance(body["runs"]["in_flight"], int)
    assert body["database"]["pool_size"] >= 1
    assert body["circuits"]["thresholds"] == {
        "failure_threshold": circuit.FAILURE_THRESHOLD,
        "open_seconds": circuit.OPEN_SECONDS,
        "max_open_seconds": circuit.MAX_OPEN_SECONDS,
    }


def test_the_counters_follow_the_state_machine(client):
    """Opened once, refused once, probed once, re-opened: four different facts."""
    _fail(circuit.FAILURE_THRESHOLD, at=1000.0)
    assert circuit.state_of(KEY, now=1000.0)["status"] == circuit.OPEN

    with pytest.raises(Exception):
        circuit.before_call(KEY, now=1001.0)  # still inside the cooldown

    # Cooldown elapsed: this caller becomes the single probe.
    circuit.before_call(KEY, now=1000.0 + circuit.OPEN_SECONDS + 1)

    # The probe fails, so the circuit re-opens with a doubled cooldown.
    circuit.record_failure(KEY, LLMTransient("still down"), now=1032.0)

    state = circuit.state_of(KEY, now=1032.0)
    assert state["status"] == circuit.OPEN
    assert state["opened_total"] == 2, state
    assert state["half_open_total"] == 1, state
    assert state["rejected_total"] == 1, state
    assert state["failures_total"] == circuit.FAILURE_THRESHOLD + 1, state
    assert state["cooldown"] == circuit.OPEN_SECONDS * 2

    body = client.get("/api/v1/diagnostics").json()
    provider = body["circuits"]["providers"][KEY_LABEL]
    assert provider["status"] == circuit.OPEN
    assert provider["opened_total"] == 2
    assert body["circuits"]["totals"]["rejected_total"] == 1


def test_reading_the_endpoint_does_not_claim_the_probe(client):
    """The regression this test exists for: a debug call that recovers the circuit.

    ``before_call`` is what grants the trial. If the diagnostics endpoint ever
    called it -- or reset state to "take a fresh reading" -- then looking at a
    broken circuit would be a way to make it look healthy.
    """
    _fail(circuit.FAILURE_THRESHOLD, at=2000.0)
    circuit.before_call(KEY, now=2000.0 + circuit.OPEN_SECONDS + 1)  # trial granted

    before = circuit.state_of(KEY, now=2031.0)
    client.get("/api/v1/diagnostics")
    client.get("/api/v1/diagnostics")
    after = circuit.state_of(KEY, now=2031.0)

    assert after == before, "a read changed the state it was reading"
    assert after["half_open_total"] == 1

    # And the trial is still held: a second caller is refused as a probe.
    with pytest.raises(Exception) as caught:
        circuit.before_call(KEY, now=2031.0)
    assert "probe is already in flight" in str(caught.value)


def test_totals_sum_across_providers_instead_of_averaging(client):
    other = ("https://other.test", "another-model")
    _fail(circuit.FAILURE_THRESHOLD, at=3000.0)
    circuit.record_failure(other, LLMTransient("down"), now=3000.0)

    totals = circuit.totals()
    assert totals["failures_total"] == circuit.FAILURE_THRESHOLD + 1
    assert totals["opened_total"] == 1

    body = client.get("/api/v1/diagnostics").json()
    assert set(body["circuits"]["providers"]) == {
        KEY_LABEL, "https://other.test|another-model",
    }


def test_a_healthy_process_reports_no_provider_at_all(client):
    """No circuit has been touched, so there is nothing to report -- not a fake row."""
    body = client.get("/api/v1/diagnostics").json()

    assert body["circuits"]["providers"] == {}
    assert body["circuits"]["totals"] == {
        "opened_total": 0,
        "half_open_total": 0,
        "rejected_total": 0,
        "failures_total": 0,
    }
