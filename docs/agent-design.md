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

- **No async job queue.** `POST /agent/run` is synchronous and holds the
  connection for up to the wall-clock budget. Fine for an operator or the CLI.
  A production deployment would queue and poll; the storage already exists, so
  that is additive rather than a redesign.
- **No streaming.** The trace is returned whole.
- **No multi-agent debate.** Several roles arguing would be a different feature.
  Nothing here prevents adding it, and the rubric-plus-evidence split in
  `classify_prospect` is the shape it would take.
- **`agent_service.py` is not fully decomposed.** The churn and follow-up
  functions still carry hardcoded thresholds. They are reachable through their
  own endpoints and are next, not done.
- **No sub-agents.** Delegation for context isolation is not implemented. See
  the alignment notes below for what it would take.

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
- **Sub-agents.** DSH has a first-class multi-provider delegation subsystem
  (`default maxDepth: 1`, `maxActiveSubagents: 8`, `one-shot` or `continuable`);
  Pi omits them on purpose. TradeAgent has neither, so delegation is the largest
  remaining gap against DSH. The shape it would take is clear from the research:
  a `spawn_researcher` tool with `maxDepth=1`, a read-only registry for the
  child, the child's final answer returned as the tool result, and the parent's
  context protected from the child's transcript.
- **Event-sourced session log.** Both use durable, replayable histories (Pi a
  JSONL tree with branching; DSH an event-sourced log with crash repair). Here
  `AgentRun.trace` is a JSON column — enough to audit and render a run, not
  enough to resume one. Fine at this scale; the first thing to change if runs
  ever need to be long-lived.

## Reading order

1. `app/agent/tools.py` — what a tool is, and how its schema is derived
2. `app/agent/loop.py` — the loop, and every way it can stop
3. `app/agent/context.py` — keeping a long run inside the window
4. `app/agent/trade_tools.py` — the rule "tools return facts" in practice
5. `app/agent/scan_runner.py` — the daily job, agent-driven
6. `tests/test_agent_loop.py` — the state machine and the transcript invariants
7. `tests/test_no_handrolled_scoring.py` — the guard against regression
