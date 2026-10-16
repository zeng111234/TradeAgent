"""Prove delegation works against a real provider, with the task's clock.

The unit tests replace the model, so they prove the wiring, not that a real
provider accepts it. This drives one delegation end to end: the child inherits
the parent's deadline and the parent's per-call ceiling, and it has to come back
with an answer rather than being cancelled.

    set OPENAI_API_KEY=...  OPENAI_BASE_URL=https://api.deepseek.com
    set OPENAI_MODEL=deepseek-chat
    python scripts/smoke_delegation.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# The tools log every SQL statement at INFO; the run's own output is what matters.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

from app.agent.service import build_llm_client, build_registry  # noqa: E402
from app.agent.subagent import DEFAULT_UNWIND_SECONDS, attach_delegation  # noqa: E402

QUESTION = (
    "Which of the customers in the CRM has the strongest evidence of an "
    "outstanding request, and what is that request? Cite the customer id, the "
    "stage and the note you relied on."
)

# Deliberately generous: this is a smoke test, not a budget test. The point is
# that the child inherits *a* deadline and finishes inside it.
PARENT_CEILING_SECONDS = 120.0


async def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("VERDICT: SKIP — no OPENAI_API_KEY in the environment")
        return 0

    client = build_llm_client()
    registry = build_registry()
    tools, record = attach_delegation(registry, llm=client)

    deadline = time.monotonic() + PARENT_CEILING_SECONDS
    started = time.monotonic()

    result, elapsed = await tools.call(
        "spawn_researcher",
        {"question": QUESTION},
        host={"_deadline": deadline, "_tool_timeout": PARENT_CEILING_SECONDS},
    )

    wall = time.monotonic() - started
    totals = record.as_dict()

    print(f"ok                     : {result.ok}")
    print(f"wall clock             : {wall:.1f}s "
          f"(ceiling {PARENT_CEILING_SECONDS}s, unwind margin {DEFAULT_UNWIND_SECONDS}s)")
    print(f"inside the ceiling     : {wall <= PARENT_CEILING_SECONDS}")
    print(f"delegations            : {totals['spawned']} spawned, {totals['refused']} refused")
    print(f"child tool calls       : {totals['tool_calls']}")
    print(f"child tokens           : {totals['tokens']}")
    print(f"child elapsed          : {totals['elapsed']}s")

    for child in record.children:
        print(f"  child status         : {child.run.status if child.run else None} "
              f"/ {child.run.stop_reason if child.run else child.error}")
        if child.run:
            print(f"  child tool sequence  : {' -> '.join(child.run.tool_sequence) or '(none)'}")
    answer = (result.raw or {}).get("answer", "")
    print(f"answer ({len(answer)} chars)      : {answer[:220].replace(chr(10), ' ')}")

    passed = (
        result.ok
        and wall <= PARENT_CEILING_SECONDS
        and totals["spawned"] == 1
        and totals["tool_calls"] > 0
        and len(answer) > 0
    )
    print(f"VERDICT: {'PASS' if passed else 'FAIL'} — "
          f"a sub-agent ran under the parent's deadline and reported back")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
