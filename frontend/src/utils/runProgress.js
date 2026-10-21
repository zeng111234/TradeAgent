/**
 * Progress shapes for a running agent.
 *
 * Pure, and out of the component on purpose: this is the part with decisions in
 * it, and the part that was wrong in the first version of the polling UI. Both
 * functions exist because of a specific defect:
 *
 * - the timeline went backwards when a slow snapshot landed after a fast one;
 * - the result panel rendered `undefined` for three fields, because the detail
 *   endpoint spells them differently from the loop's own result object.
 */

/** Statuses the loop can stop in. Anything else means it is still working. */
export const TERMINAL_STATUSES = Object.freeze([
  'succeeded',
  'truncated',
  'stalled',
  'error',
  // Stopped on purpose. Terminal, but not a failure: the rounds it finished
  // are still a valid record and are shown.
  'cancelled',
])

export function isTerminal(status) {
  return TERMINAL_STATUSES.includes(status)
}

/**
 * What a finished run should be called, in one place.
 *
 * `truncated` is a budget stop, not a failure; a `succeeded` run can still have
 * refused tool calls because one budget ran out mid-round. Both are "budget",
 * which reads differently from "failure" and must not borrow its red.
 */
export function runOutcome(status, stopCode = '', refusedCalls = 0) {
  if (status === 'starting' || status === 'running' || status === 'cancelling') return 'running'
  if (status === 'cancelled') return 'cancelled'
  if (status === 'error' || status === 'stalled') return 'failure'
  if (status === 'truncated') return 'budget'
  if (status === 'succeeded') return refuseCount(refusedCalls) > 0 || stopCode === 'token_budget'
    ? 'budget'
    : 'success'
  return 'success'
}

function refuseCount(value) {
  return Number.isFinite(Number(value)) ? Number(value) : 0
}

/**
 * Money, or a dash. Never `$0.00` for an unknown price.
 *
 * ``cost`` is ``null`` when nobody declared a price for the model or when a
 * round did not report usage. Both mean unknown, and printing zero would state
 * the opposite of what the server said.
 */
export function formatCost(cost) {
  if (!cost || typeof cost.amount !== 'number' || !Number.isFinite(cost.amount)) return '—'
  const amount = cost.amount
  const digits = amount > 0 && amount < 0.01 ? 4 : amount < 1 ? 3 : 2
  const symbol = cost.currency === 'USD' ? '$' : cost.currency === 'CNY' ? '¥' : ''
  const rendered = amount.toFixed(digits)
  const suffix = cost.estimated ? '~' : ''
  return symbol ? `${suffix}${symbol}${rendered}` : `${suffix}${rendered} ${cost.currency || ''}`.trim()
}

/** 12345 -> "12.3k". Used for token counts, where exact digits are noise. */
export function formatTokens(total) {
  const n = Number(total) || 0
  if (n < 1000) return String(n)
  const k = n / 1000
  return `${k >= 10 ? Math.round(k) : k.toFixed(1)}k`
}

/**
 * Union by round, keeping the fuller version of a round seen twice.
 *
 * The server rewrites the whole trace on each progress write, so a later
 * snapshot is normally a superset of the previous one. Assigning it directly
 * would still be wrong once in a while: two progress writes are separate tasks,
 * a slow one can land after a fast one, and the trace column itself can briefly
 * move backwards. The final write (when the run ends) is complete, but a poller
 * can read the middle.
 *
 * Two rules, and the second one is why `>=` rather than `>`: a round seen with
 * two tool calls must not be replaced by the same round with one.
 */
export function mergeSteps(current, incoming) {
  if (!incoming || !incoming.length) return current || []
  if (!current || !current.length) return incoming
  const byRound = new Map(current.map((step) => [step.round, step]))
  for (const step of incoming) {
    const previous = byRound.get(step.round)
    const moreCalls = (step.tool_calls?.length || 0) >= (previous?.tool_calls?.length || 0)
    if (!previous || moreCalls) byRound.set(step.round, step)
  }
  return [...byRound.values()].sort((a, b) => a.round - b.round)
}

/**
 * Adapt a `GET /agent/runs/{id}` body to what the result panel renders.
 *
 * The panel was written against the shape the loop returns directly
 * (`tool_sequence`, `did_investigate`, `elapsed` in seconds). The detail
 * endpoint returns `steps`, `elapsed_ms`, and neither of the other two.
 *
 * The two elapsed fields do **not** share a unit:
 *
 * - `elapsed_ms` is milliseconds. ``repository.save_run`` stores
 *   ``int(payload["elapsed"] * 1000)``, and the snapshot passes the column
 *   through under that name.
 * - ``elapsed`` (from the SSE progress event) is seconds; the stream divides
 *   the column by 1000 before sending it.
 *
 * Treating both as seconds is how a six-second run rendered as "6000s". The
 * adapter converts only the millisecond field and leaves the seconds field
 * alone. Renaming the API field is a breaking change for every client (R49),
 * so the fix belongs here, not in the response shape.
 */
export function normaliseRun(snapshot) {
  const steps = snapshot?.steps || []
  const toolSequence = steps.flatMap((step) =>
    (step.tool_calls || []).map((call) => call.name)
  )
  const hasMillis = snapshot?.elapsed_ms !== null && snapshot?.elapsed_ms !== undefined
  return {
    ...snapshot,
    steps,
    elapsed: hasMillis ? snapshot.elapsed_ms / 1000 : (snapshot?.elapsed ?? 0),
    tool_sequence: snapshot?.tool_sequence || toolSequence,
    // ``did_investigate`` is "at least one tool was actually called", which is
    // exactly what the steps show. Deriving it here keeps the badge honest for
    // a run that answered from memory.
    did_investigate: snapshot?.did_investigate ?? toolSequence.length > 0,
  }
}
