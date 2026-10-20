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
])

export function isTerminal(status) {
  return TERMINAL_STATUSES.includes(status)
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
 * `elapsed_ms` holds **seconds**, despite the name: it is assigned from
 * `row.elapsed`, and the loop writes `round(self.elapsed, 3)`. Renaming a field
 * is a breaking change for every client (R49), so this adapter treats it as
 * seconds and the mismatch is recorded rather than papered over.
 */
export function normaliseRun(snapshot) {
  const steps = snapshot?.steps || []
  const toolSequence = steps.flatMap((step) =>
    (step.tool_calls || []).map((call) => call.name)
  )
  return {
    ...snapshot,
    steps,
    elapsed: snapshot?.elapsed_ms ?? snapshot?.elapsed ?? 0,
    tool_sequence: snapshot?.tool_sequence || toolSequence,
    // ``did_investigate`` is "at least one tool was actually called", which is
    // exactly what the steps show. Deriving it here keeps the badge honest for
    // a run that answered from memory.
    did_investigate: snapshot?.did_investigate ?? toolSequence.length > 0,
  }
}
