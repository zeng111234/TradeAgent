# Agent design

Why this project is now an agent, what changed, and what it deliberately still
cannot do.

## The problem, stated precisely

`README.md` calls this TradeAgent. Until this refactor the LLM never chose
anything. Every decision lived in Python, and the model was called once, at the
end, to produce prose.

Three places made that concrete.

**1. No tool calling.** Every model call looked like this:

```python
response = await client.chat.completions.create(
    model=..., messages=[...], temperature=..., max_tokens=...,
)
content = response.choices[0].message.content    # read the text, done
```

No `tools` argument, no `tool_calls` in the reply. The model could talk. It
could not act.

**2. The judgement was a formula.** `scripts/daily_scan.py` decided which
prospects existed:

```python
score = 20
if is_b2b:              score += 30
if buyer_keyword:       score += 20
if product_keyword:     score += 20
if emails_found:        score += 10
if score >= 40:         keep()
```

`agent_service.auto_lead_scanner` had a larger version, and worse: it ran
*before* the model was called, so the model ranked a shortlist it had no say in.
The surviving number was displayed as `/100` next to a real lead, which reads as
an assessment rather than as four constants somebody once picked.

**3. The control flow was Python.** `workflows.py` opened with *"the 'brain'
that connects all tools"* while being a fixed `await` sequence.
`scheduler.py` opened with *"the 'brain' that runs workflows on a schedule"*
while being an APScheduler timer. Both comments described an intention the code
did not implement.

## What an agent needs, and what was added

An agent needs four things. Each maps to a module.

| Requirement | Module |
|---|---|
| A protocol for tool use | `app/agent/protocol.py` |
| A bounded loop that reads results and decides again | `app/agent/loop.py` |
| Tools, with schemas derived from their code | `app/agent/tools.py`, `trade_tools.py`, `scan_tools.py` |
| A trace of what actually happened | `app/models/agent_run.py`, `repository.py`, `render.py` |

### Tool schemas cannot drift from the implementation

A tool is an annotated async function plus a Pydantic model. The model is
generated from the signature:

```python
@tool(description="Raw numeric signals for one customer", tags={"read", "crm"})
async def get_customer_signals(customer_id: int) -> dict:
    ...
```

Pydantic v2 emits exactly the shape `tools` needs — `required` lists only the
fields without defaults — so there is no hand-written JSON to fall out of sync.
Adding a parameter is the only step.

### The loop is bounded on four axes

`AgentBudget` caps rounds, tool calls, tokens and wall-clock. A per-round cap
stops one round from firing twenty calls, and each tool has its own timeout.
An agent that can run forever is an outage with a nice user interface.

### One task is a run tree, not a loop

The unit of work is **one goal, one run id, one deadline**. Everything the run
spawns belongs to it.

That distinction has teeth, because each loop carries its own ceilings and a
loop that delegates starts more of them. Measured with a one-second parent
budget and two delegations in one round, before this was fixed:

| | value |
|---|---|
| parent `max_seconds` | 1.0 |
| parent `run.elapsed` | **2.42** |
| what the parent reported | 1 tool call, 230 tokens |
| what the task actually did | 3 tool calls, 1150 tokens |

A round is charged against the parent's clock only at its start, so every
delegation inside it is free. Scaled to the presets, `180 + 5 x 90 = 630`
seconds for a task the API promised would take at most 180.

So the deadline is inherited rather than re-derived:

* The loop computes one absolute deadline, `start + max_seconds`, and a child
  that already has one keeps the earlier of the two.
* The parent passes it to tools as a **host parameter** (`_deadline`), invisible
  in the schema and unforgeable in the model's arguments — a deadline the model
  could write is not a deadline.
* A child's allowance is the smallest of its own ceiling, what the parent has
  left, and the parent's per-call ceiling — the last of which it previously
  exceeded, so a 90-second allowance behind a 60-second call cap was always
  cancelled at 60 and lost everything it had found.
* A tool's own timeout is clamped to what remains, and a call left in a round
  after the deadline is refused with `run_deadline_reached` rather than run.
* Cancellation, if it still happens, is recorded as cancellation instead of
  leaving a spawned child with no run and no reason.

The counts stay per loop, because that is what each number means; the **task**
totals are reported alongside them (`tool_calls_total`, `tokens_total`), and the
trace header says when sub-agents are excluded from the figures above it.

### Failure is data, not an exception

This is the property that matters most in practice:

| Situation | Behaviour |
|---|---|
| Tool raises | Captured, returned as an observation the model can react to |
| Tool name does not exist | Observation listing the tools that do exist |
| Arguments are not valid JSON | Observation quoting the bad input, so the model can retry |
| Model calls nothing, repeatedly | Run ends `stalled` with the reason |
| Model writes prose without investigating | Nudged, not accepted as an answer |

A hostile or broken response from a scraped website must not be able to crash a
run. It should be something the agent can read and decide about.

### Text without investigation is not an answer

If the model replies "the best prospects are in Germany" having called no tools,
that is not a conclusion, it is a guess with confident phrasing. The loop asks
it to gather evidence first. Only a substantive reply, or any reply after real
tool use, ends the run — and `did_investigate` records which it was.

## Tools return facts; the model makes judgements

This is the rule the whole design is built around.

`get_customer_signals` returns numbers — days since contact, days in stage,
counts of emails sent/opened/replied. It returns **no risk label**. The code it
replaced hardcoded both the thresholds and the verdicts:

```
3 emails sent + 0 replies  -> "critical"
14 days in quoting         -> "high"
30 days of silence         -> "medium"
```

Those are opinions frozen at a point in time. They should be argued with, not
inherited.

`classify_prospect` makes the same choice visible from the other side. It
returns a **rubric** and the page text to judge, and deliberately produces **no
score**. The agent supplies the score and the reasoning, so every number in a
run is traceable to a model turn rather than to a constant.

When the data needed for a judgement is missing, the honest output is an
explicit gap rather than a number. `_basic_analyze_website` — the no-model
fallback — no longer emits a `lead_score` at all. It returns `not_analyzed` and
says plainly that nothing assessed the page. An honest gap beats a fabricated
number, because a number gets acted on.

## Authority is enforced by omission

The agent can investigate, read, and prepare drafts. It cannot dispatch.

There is no send tool, no delete tool, and no tool that modifies an existing
customer or email log. `create_email_draft` stores a draft with status
`pending`, which is what the existing two-step review flow already consumes.

Prompt wording is not the mechanism. The mechanism is that the capability does
not exist in the registry, which `tests/test_agent_tools_policy.py` asserts:

- no registered tool name matches `send`, `dispatch`, `delete`, `remove`,
  `drop`, `purge`
- the agent package contains no SMTP import at all
- the only write-capable tools are `create_email_draft` and `upsert_customer`

`upsert_customer` refuses to modify an existing row and reports `skipped`
instead. An agent silently rewriting curated data is worse than an agent adding
a duplicate you can delete.

## The trace is the point

Every round records the model's text, each tool call with its arguments, the
observation and the latency, plus token counts. `AgentRun` stores it; nothing is
reconstructed afterwards.

`render.py` turns a trace into a Mermaid sequence diagram or flowchart, and a
standalone HTML page. It is templating only — no model call, no network — so the
same trace always produces the same diagram, and the diagram cannot flatter the
run.

**This is also why the workflow does not need to be drawn in n8n.** The diagram
is generated from what the run actually did. A canvas shows what somebody
remembered to draw; this shows which tool ran, with which arguments, and what
came back. Note also that scraped page text flows into these labels, so the
renderer treats observations as untrusted input and there are tests for newline
injection.

## Live output, stopping, and threads

Three things a chat window needs that a per-round trace cannot provide alone.

**Live output.** The loop writes one trace step per round, so between rounds the
database has nothing new. `app/agent/live.py` buffers the provider's deltas in
process; `/events` drains them on a 100 ms tick while continuing to send the
authoritative round snapshots once a second. The division is deliberate: a
`delta` is best effort and carries **no SSE `id`**, because `id` is the resume
cursor and a partial round must not move it. A reconnect gets one `live` frame
with the round's full text, then deltas — a dropped delta costs a repaint, never
a wrong transcript. When the run is not local (another worker, or after a
restart) the endpoint sends snapshots only, and the UI degrades to per-round
updates.

**Stopping.** `POST /runs/{id}/cancel` sets a `CancelToken`; the loop checks it
between rounds, between tool calls, and races it against the in-flight model
call. It is not `Task.cancel()`: `CancelledError` is a `BaseException`, so the
background runner's `except Exception` would miss it, leaving the row `running`
until the stale sweep and discarding every completed round. A cancelled run is
terminal, keeps its steps, leaves `final_output` empty (half a sentence is not
an answer), and refuses the remaining calls with `reason=cancelled`. A tool
already running is allowed to finish: "stop" is not "undo".

**Threads.** `agent_conversations` names a thread; `agent_runs.conversation_id`
joins it. A follow-up is given the last three answered turns as real
user/assistant messages — not a system blob — and compaction protects them by
count, because trimming history to 80-character fragments looks like context
and carries none. Only answered runs are remembered; an errored turn has
nothing worth carrying forward.

All three are in-process by design. `_in_flight`, the usage ledger and the live
buffer already assume one uvicorn worker; a multi-worker deployment degrades to
"snapshots, no deltas, cancel returns `not_local`" rather than corrupting data.

## Acceptance: two goals, two paths

The load-bearing claim is that the loop does not know which tools suit which
goal. `scripts/demo_agent_run.py` checks it and exits non-zero if it fails:

```
find new buyers    search_prospects -> inspect_website -> classify_prospect
review pipeline    list_customers -> get_customer_signals -> get_communication_history
```

A hardcoded pipeline cannot produce this without carrying a branch for every
goal a user might type.

## What is deliberately not done

- **No durable job queue.** `POST /agent/run` starts an in-process background
  task (or, with `wait=true`, holds the connection). A restart cannot resume
  one; `mark_stale_runs` records that honestly instead of leaving a promise.
  A queue would be additive — the row and its id already exist before the work.
- **No multi-agent debate.** Several roles arguing would be a different feature.
  Nothing here prevents adding it, and the rubric-plus-evidence split in
  `classify_prospect` is the shape it would take.
- **`agent_service.py` is not fully decomposed.** The churn and follow-up
  functions still carry hardcoded thresholds. They are reachable through their
  own endpoints and are next, not done.
- **No sub-agents for the smallest runs.** Delegation is on by default but can be
  turned off per run (`allow_delegation=False`), and a scan child cannot spawn
  grandchildren.

## Alignment with production harnesses

This design was checked against two shipping agents: **Pi** (the coding agent at
pi.dev, source in `badlogic/pi-mono`) and **DeepSeek Harness**. The comparison
changed three things, and it is worth recording which and why.

### The bounded loop is the part neither reference ships

Both Pi and DSH run **unbounded** loops. Pi's `runLoop` has no step, turn, token
or wall-clock cap — zero grep hits in the loop source, and no such setting. DSH
documents the absence as deliberate:

> No built-in turn budget — tool calls or steering continue the current turn; a
> policy that bounds runaway turns must cancel from an existing lifecycle
> extension point.

DSH's own field report shows the consequence: a **single turn of 72 steps**, with
a repetition detector that never fired because it keys on *identical* arguments
and the polling loop varied one `timeout_ms`.

So `AgentBudget` is not the naive choice here — it is the piece both reference
agents leave to the implementer. Keeping it.

### Adopted: a truncated response is not a valid tool call

Pi refuses to execute *any* tool call from a message whose `stopReason` is
`length`, because salvage-parsed arguments can validate while being silently
truncated — a half-written URL or body that passes the schema and then does
damage.

`AgentLoop` now does the same. On `finish_reason` of `length` or `max_tokens`,
every call in that round is refused with an explanation and the model is told to
re-issue. Covered by
`test_calls_from_a_length_truncated_response_are_refused`.

### Adopted: every declared call gets exactly one result, in order

The subtler invariant, and one this loop was **actually breaking**. The assistant
message declares N tool calls; if the budget ran out mid-loop the old code
returned immediately, leaving orphaned `tool_call`s with no results. DSH exists
specifically to prevent this — `TOOL_OUTCOME_UNKNOWN` / `TOOL_NOT_STARTED` keep
the transcript replayable, and its compaction cuts snap to
`toolPairingBalancedBefore/After` edges.

The first fix produced a second bug that a test caught: refused results were
appended *before* executing the allowed ones, so results arrived as
`[2, 3, 0, 1]`. Reordered results are their own hazard — a model replaying the
transcript reads them against the wrong calls. The batch is now split first and
appended strictly in declaration order.

Both halves are asserted by `_assert_pairing_holds`, which compares the
*sequence* of declared ids against the sequence of result ids rather than
counting them. `test_agent_loop.py` runs that check over every termination path:
normal, budget-exhausted, mid-batch cut, stall, and round-budget.

### Adopted: truncate, but keep it retrievable

`context.py` bounds the request. Pi's convention is *"truncate large model-facing
results and tell the model where to read the complete output"*; DSH goes further
with a spill policy that writes the full result to a file and hands the model a
path, summarised as *"truncate but preserve recoverability."*

`SpillStore` does the same: trimmed observations are written to
`AGENT_SPILL_DIR` (default: system temp), the marker names the file, and
`recall_observation` reads it back. The tool is registered **only when a spill
directory exists**, so the marker never promises a retrieval that cannot happen.

The first implementation of the cut was wrong in a way worth recording. A fixed
head/tail size frees a constant number of tokens per pass; if each round adds
more than that, the request grows without bound while every pass reports success.
Measured: at round 8 the request was **4696 tokens against a 3000 budget, still
climbing**. The cut is now scaled to the deficit — how much the trimmable middle
must shrink — which holds 14 fat rounds to a peak of **2880**. Past a floor the
middle cannot shrink further and the report sets `unreachable`, rather than
claiming a bound it did not achieve.

### Compared, and deliberately not adopted

- **Approval escalation.** DSH has a four-layer fail-closed stack: a closed
  outcome vocabulary (`allowed-once` / `rejected` / `cancelled` / `unavailable`),
  a session `ask`/`never` policy enforced *before* the answerer chain, per-call
  sandbox modes with a closed escalation ladder requiring a justification, and
  audit events kept out of model context. Pi has **no permission system at all**
  and relies on OS isolation plus a `tool_call` extension convention.
  TradeAgent's boundary is different in kind: it does not need to mediate
  arbitrary filesystem or process access, it needs to not send email. Omitting
  the capability from the registry achieves that with no policy engine to get
  wrong. The trade-off is real, though — a whitelist cannot express "read-only
  except when the operator says otherwise", and adding such a tier is the
  obvious next step if the tool set grows.
- **Sub-agents.** Implemented in `app/agent/subagent.py`. DSH has a first-class
  multi-provider delegation subsystem (`maxDepth` default 1,
  `maxActiveSubagents` 8, one-shot or continuable); Pi omits sub-agents on
  purpose. This follows DSH on the parts that matter:
  - **Depth is enforced, not advised.** `maxDepth` defaults to 1. At the cap the
    tool stays *visible* and the call is refused with a reason — hiding it would
    leave the model unable to tell that delegation exists but is exhausted.
    `ToolRegistry.depth` is what makes this checkable rather than advisory.
  - **A child gets its own registry**, built fresh from the parent's minus the
    delegation tools and minus anything tagged `write`. A child that could reach
    a scan's `save_prospects` would be writing into the parent's output from a
    context the parent cannot see.
  - **The child's final answer is the tool result**, plus a small
    `how_it_was_established` block so the parent can weigh it. The child's
    transcript stays in its own run record.
  - **A child failure is a tool failure.** It cannot abort the parent.
- **Event-sourced session log.** Both use durable, replayable histories (Pi a
  JSONL tree with branching; DSH an event-sourced log with crash repair). Here
  `AgentRun.trace` is a JSON column — enough to audit and render a run, not
  enough to resume one. Fine at this scale; the first thing to change if runs
  ever need to be long-lived.

### Where this is deliberately simpler than DSH

- **Sub-agents run sequentially, not concurrently.** DSH admits up to 8 live
  children; here it is one at a time. Enough for the use case and it avoids a
  class of interleaving bugs. The limit is a choice, not an oversight.
- **No `continuable` children.** Every child is one-shot. A durable child that
  can be steered later is a larger feature than this needs.
- **No session `ask`/`never` policy.** See the approval note above: the
  capability is absent from the registry, so there is nothing to approve.

## Reading order

1. `app/agent/tools.py` — what a tool is, and how its schema is derived
2. `app/agent/loop.py` — the loop, and every way it can stop
3. `app/agent/context.py` — keeping a long run inside the window
4. `app/agent/trade_tools.py` — the rule "tools return facts" in practice
5. `app/agent/scan_runner.py` — the daily job, agent-driven
6. `tests/test_agent_loop.py` — the state machine and the transcript invariants
7. `tests/test_no_handrolled_scoring.py` — the guard against regression
