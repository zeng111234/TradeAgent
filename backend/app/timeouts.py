"""Every timeout in this service, and the rule that makes them add up.

One timeout is not enough. There are five layers here, and each of them has a
different job:

1. **per attempt** -- one HTTP request to a model, one SMTP message;
2. **per logical call** -- a request *including* its retries. The SDK retries
   twice with backoff, so a 120-second attempt timeout is a 360-second call;
3. **per unit of work** -- one agent run, one batch send. These are budgets, not
   timeouts: they bound a loop of unknown length;
4. **per request** -- what the browser is willing to wait for;
5. **per resource** -- how long to wait for a pooled connection, or for SQLite's
   single writer.

The rule: **each layer is strictly shorter than the layer that waits on it.**
When that is inverted, the outer layer gives up first, and the work it abandoned
either keeps running unwatched or is thrown away -- and the error the user sees
names the wrong layer. Three inversions existed in this codebase and are now
fixed (see the tests in ``tests/test_timeout_ladder.py``):

* a child agent's 90-second allowance was smaller than its model client's
  120-second request timeout, so one call could outlast the whole child;
* ``wait=true`` accepted a nine-hundred-second run while the browser gave up at
  a hundred and twenty;
* one SMTP message was bounded at sixty seconds, but a batch of fifty was not
  bounded at all -- fifty minutes inside a process-wide lock.

These numbers are policy, not physics. They are here so that the next person
changing one can see what it has to stay under.
"""
from __future__ import annotations

#: What the browser waits for (``frontend/src/api/index.js``). Not enforceable
#: from here; it is the ceiling everything else has to fit under.
BROWSER_REQUEST_SECONDS = 120.0

#: ``POST /agent/run`` with ``wait=true`` holds the HTTP connection, so it has to
#: finish inside the browser's patience with room to spare for the response.
#: Longer work must use ``wait=false`` -- the row exists up front, so polling
#: works from the first moment.
WAIT_MODE_MAX_SECONDS = 100.0

#: A whole agent run: the budget the loop enforces across all rounds and all
#: sub-agents. The API accepts up to 900 for ``wait=false`` runs.
RUN_MAX_SECONDS = 180.0
RUN_ABSOLUTE_MAX_SECONDS = 900.0

#: One tool call inside a run, and one sub-agent's slice of the parent's budget.
TOOL_CALL_SECONDS = 60.0
SUBAGENT_ALLOWANCE_SECONDS = 90.0

#: One model request, and one *logical* model call.
#:
#: The order between these two is the point: the SDK retries *inside* a logical
#: call, so a per-attempt timeout larger than the total is a contradiction -- the
#: attempt could never be allowed to finish, and the caller is cancelled at the
#: total anyway. Per attempt 60s, total 80s: at most one attempt completes, a
#: second only if the first failed fast.
LLM_REQUEST_SECONDS = 60.0
LLM_MAX_RETRIES = 2
LLM_TOTAL_SECONDS = 80.0

#: Blocking I/O on a worker thread, and SMTP.
PAGE_FETCH_SECONDS = 15.0
SMTP_SEND_SECONDS = 30.0
#: A whole ``POST /emails/send-pending`` call. The batch is bounded by count
#: (``DEFAULT_BATCH_LIMIT``) and by this; whichever comes first stops it, and the
#: response says how many are left. Under the browser's patience, because the UI
#: calls this one request at a time.
BATCH_SEND_BUDGET_SECONDS = 90.0

#: How long to wait for a pooled connection, and for SQLite's single writer.
#: The second is larger on purpose: once a connection is in hand, waiting for the
#: writer is real work rather than queueing.
POOL_CHECKOUT_SECONDS = 5.0
SQLITE_BUSY_SECONDS = 30.0
