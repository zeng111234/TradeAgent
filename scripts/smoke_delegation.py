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


async def _prepare_database() -> int:
    """Create and seed the book, then return how many customers it holds.

    Without this the child truthfully reports "the CRM is not readable", the
    verdict still passes because it only checks the deadline and the spawn count,
    and the smoke stops proving the tool path it was written for. Found by
    running it against a throwaway database: seven tool calls, all failing on
    ``no such table: customers``.
    """
    from sqlalchemy import func, select

    from app.database import async_session, init_db
    from app.main import _insert_demo_data
    from app.models.customer import Customer

    await init_db()
    await _insert_demo_data()

    async with async_session() as session:
        return (await session.execute(select(func.count(Customer.id)))).scalar() or 0


async def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("VERDICT: SKIP — no OPENAI_API_KEY in the environment")
        return 0

    book_size = await _prepare_database()
    if book_size == 0:
        # A delegation over an empty book can "succeed" while proving nothing
        # about the tools. Fail here, not in the verdict's blind spot.
        print("VERDICT: FAIL — the CRM is empty; delegation over no data proves nothing")
        return 1

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

    # A call that was executed but failed still counts in ``totals['tool_calls']``.
    # The verdict needs "the child got data", not "the child tried".
    successful_calls = sum(
        1
        for child in record.children
        if child.run
        for step in child.run.steps
        for call in step.tool_calls
        if call.ok and not call.refused
    )

    print(f"ok                     : {result.ok}")
    print(f"wall clock             : {wall:.1f}s "
          f"(ceiling {PARENT_CEILING_SECONDS}s, unwind margin {DEFAULT_UNWIND_SECONDS}s)")
    print(f"inside the ceiling     : {wall <= PARENT_CEILING_SECONDS}")
    print(f"delegations            : {totals['spawned']} spawned, {totals['refused']} refused")
    print(f"child tool calls       : {totals['tool_calls']}")
    print(f"child calls succeeded  : {successful_calls}")
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
        and successful_calls > 0
        and len(answer) > 0
    )
    print(f"VERDICT: {'PASS' if passed else 'FAIL'} — "
          f"a sub-agent ran under the parent's deadline and reported back")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
