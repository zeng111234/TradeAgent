"""Read-only process diagnostics: what the system thinks is true right now.

Why this endpoint exists
------------------------
Three numbers decided whether a request is served or refused -- the circuit
breaker's state, the in-flight run count, and how many database connections are
checked out -- and none of them could be read without attaching a debugger. A
stuck circuit in particular is invisible: every caller sees the same
"provider unavailable" and nobody can tell "it opened once, minutes ago" from
"it has opened eleven times this minute".

What it is not
--------------
* **Not a metric store.** These are gauges and counters since process start.
  History lives in ``/analytics/quality``; this is "right now".
* **Not a write path.** Nothing here claims a trial, resets a circuit, or takes a
  connection it does not immediately return. A diagnostics call during an
  incident must not change what it is measuring.
* **Not authenticated.** Same posture as ``/health`` and ``/ready``: this service
  is a local tool. The strings it exposes are a model name, a base URL and a
  truncated provider error -- no keys. If it is ever put behind a public
  hostname, this endpoint belongs behind the same door as the rest of the API.
"""
from datetime import datetime, timezone

from fastapi import APIRouter

from app import circuit
from app.database import engine
from app.routers import agent_runs

router = APIRouter(prefix="/diagnostics", tags=["Diagnostics"])


@router.get("")
async def diagnostics():
    """Circuit states, run slots and pool occupancy, in one read-only call."""
    pool = engine.pool
    try:
        checked_out = pool.checkedout()
        pool_size = pool.size()
    except (AttributeError, NotImplementedError):  # pragma: no cover - pool swap
        # A different pool class (StaticPool for an in-memory database) does not
        # implement these. Reporting ``null`` is honest; raising would make the
        # whole endpoint unusable in exactly the environment tests run in.
        checked_out = pool_size = None

    return {
        "circuits": {
            "providers": circuit.snapshot(),
            "totals": circuit.totals(),
            #: The values these counters are measured against. Without them a
            #: reader cannot tell 5 openings in a day from 5 in a minute.
            "thresholds": {
                "failure_threshold": circuit.FAILURE_THRESHOLD,
                "open_seconds": circuit.OPEN_SECONDS,
                "max_open_seconds": circuit.MAX_OPEN_SECONDS,
            },
        },
        "runs": {
            "in_flight": len(agent_runs._in_flight),
            "limit": agent_runs.MAX_CONCURRENT_RUNS,
        },
        "database": {
            "pool_checked_out": checked_out,
            "pool_size": pool_size,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
