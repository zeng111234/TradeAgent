"""Fault injection for the circuit breaker: no provider, no tokens, no network.

What this answers
-----------------
``FAILURE_THRESHOLD = 5`` and ``OPEN_SECONDS = 30`` were chosen from the call
volume, not measured against an outage. Nobody could see the state machine work
end to end without a provider that actually fails, and a provider that actually
fails costs money and time to arrange.

So this drives the breaker with a fake clock and a constructed error, and prints
the timeline a human would otherwise have to infer from logs: how many failures
it took to open, how long a refusal lasts, that exactly one probe is let through,
that a failed probe doubles the cooldown, and that a successful probe closes it.

It is a *check*, not a benchmark: every transition below is an assertion about
the design, and a mismatch is exit code 2. Run it after changing a threshold.

    python scripts/circuit_probe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app import circuit  # noqa: E402
from app.llm import LLMCircuitOpen, LLMTransient  # noqa: E402

KEY = ("https://fake-provider.invalid", "fake-model")

#: A constructed failure the breaker classifies as RETRY -- the same family a
#: 503 or a timeout lands in. Not a real exception from a real SDK: this script
#: must not depend on the network being down to test what happens when it is.
DOWN = LLMTransient("the provider returned HTTP 503")

rows: list[tuple[str, str, str]] = []
problems: list[str] = []


def expect(label: str, actual, wanted) -> None:
    ok = actual == wanted
    rows.append((label, repr(actual), ("ok  " if ok else "FAIL") + f" expected {wanted!r}"))
    if not ok:
        problems.append(f"{label}: got {actual!r}, expected {wanted!r}")


def main() -> int:
    circuit.reset()
    clock = 1_000.0

    print("circuit fault injection (fake clock, fake provider)\n")
    print(
        f"  threshold={circuit.FAILURE_THRESHOLD}  "
        f"open={circuit.OPEN_SECONDS:.0f}s  max={circuit.MAX_OPEN_SECONDS:.0f}s\n"
    )

    # --- just below the threshold: still closed ---------------------------
    for _ in range(circuit.FAILURE_THRESHOLD - 1):
        circuit.record_failure(KEY, DOWN, now=clock)
    expect(
        f"{circuit.FAILURE_THRESHOLD - 1} failures stay closed",
        circuit.state_of(KEY, now=clock)["status"],
        circuit.CLOSED,
    )

    # --- the next failure opens it ----------------------------------------
    circuit.record_failure(KEY, DOWN, now=clock)
    state = circuit.state_of(KEY, now=clock)
    expect(f"failure #{circuit.FAILURE_THRESHOLD} opens", state["status"], circuit.OPEN)
    expect("opened_total counts the transition", state["opened_total"], 1)
    expect(
        "cooldown starts at the configured value",
        state["cooldown"],
        circuit.OPEN_SECONDS,
    )

    # --- while open, calls are refused without being attempted -----------
    refused = False
    try:
        circuit.before_call(KEY, now=clock + 1)
    except LLMCircuitOpen:
        refused = True
    expect("a call inside the cooldown is refused", refused, True)
    expect("and is counted", circuit.state_of(KEY, now=clock + 1)["rejected_total"], 1)

    # --- after the cooldown exactly one probe is let through -------------
    clock += circuit.OPEN_SECONDS + 1
    circuit.before_call(KEY, now=clock)
    expect(
        "the first caller after the cooldown probes",
        circuit.state_of(KEY, now=clock)["status"],
        circuit.HALF_OPEN,
    )

    second_probe = False
    try:
        circuit.before_call(KEY, now=clock + 0.1)
    except LLMCircuitOpen as exc:
        second_probe = "probe is already in flight" in str(exc)
    expect("a second caller is refused while the probe runs", second_probe, True)

    # --- the probe fails: re-open, cooldown doubles ----------------------
    circuit.record_failure(KEY, DOWN, now=clock + 0.2)
    state = circuit.state_of(KEY, now=clock + 0.2)
    expect("a failed probe re-opens the circuit", state["status"], circuit.OPEN)
    expect("and doubles the cooldown", state["cooldown"], circuit.OPEN_SECONDS * 2)
    expect("opened_total counts the second opening", state["opened_total"], 2)

    # --- the next probe succeeds: close and reset ------------------------
    clock += circuit.OPEN_SECONDS * 2 + 1
    circuit.before_call(KEY, now=clock)
    circuit.record_success(KEY, now=clock)
    state = circuit.state_of(KEY, now=clock)
    expect("a successful probe closes the circuit", state["status"], circuit.CLOSED)
    expect("the failure count is cleared", state["consecutive_failures"], 0)
    expect(
        "the cooldown is back to the configured value",
        state["cooldown"],
        circuit.OPEN_SECONDS,
    )
    expect("both trials were counted", state["half_open_total"], 2)

    for label, observed, verdict in rows:
        print(f"  {label:52} {observed:>10}   {verdict}")

    print(f"\n  totals: {circuit.totals()}")

    circuit.reset()
    if problems:
        print(f"\nVERDICT: FAIL - {len(problems)} transition(s) did not match the design")
        for line in problems:
            print(f"  - {line}")
        return 2
    print("\nVERDICT: PASS - every transition matches the configured policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
