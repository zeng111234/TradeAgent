/**
 * The two decisions in the polling UI, run without a browser.
 *
 * No component is mounted here. `mergeSteps` and `normaliseRun` are pure and
 * the rest of the view is wiring; the bugs worth pinning were both in these
 * two functions, not in the template.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { isTerminal, mergeSteps, normaliseRun } from '../src/utils/runProgress.js'
import { formatCost, formatTokens, runOutcome } from '../src/utils/runProgress.js'

const step = (round, calls = []) => ({
  round,
  thought: `round ${round}`,
  tool_calls: calls.map((name, index) => ({ name, ok: true, elapsed: 0.1 * index, arguments: {}, observation: 'ok' })),
})

test('a slow snapshot arriving last does not remove rounds', () => {
  const seenThree = [step(1), step(2), step(3)]
  const lateTwo = [step(1), step(2)]

  assert.deepEqual(
    mergeSteps(seenThree, lateTwo).map((s) => s.round),
    [1, 2, 3],
    'the timeline went backwards'
  )
})

test('a round seen twice keeps the version with more tool calls', () => {
  const partial = [step(1, ['search_leads'])]
  const fuller = [step(1, ['search_leads', 'inspect_website'])]

  const merged = mergeSteps(partial, fuller)

  assert.equal(merged.length, 1)
  assert.equal(merged[0].tool_calls.length, 2)
})

test('a round is never replaced by a less complete copy', () => {
  const fuller = [step(1, ['a', 'b'])]
  const partial = [step(1, ['a'])]

  assert.equal(mergeSteps(fuller, partial)[0].tool_calls.length, 2)
})

test('rounds merge in round order regardless of arrival order', () => {
  const merged = mergeSteps([step(2), step(3)], [step(1), step(4)])

  assert.deepEqual(merged.map((s) => s.round), [1, 2, 3, 4])
})

test('either side may be empty', () => {
  assert.deepEqual(mergeSteps([], [step(1)]).map((s) => s.round), [1])
  assert.deepEqual(mergeSteps([step(1)], []).map((s) => s.round), [1])
  assert.deepEqual(mergeSteps([], []), [])
})

test('only the loop stop statuses are terminal', () => {
  for (const status of ['succeeded', 'truncated', 'stalled', 'error']) {
    assert.equal(isTerminal(status), true, status)
  }
  for (const status of ['running', 'starting', '', undefined]) {
    assert.equal(isTerminal(status), false, String(status))
  }
})

test('the result panel gets the fields the detail endpoint does not send', () => {
  const detail = {
    run_id: 'r1',
    status: 'succeeded',
    rounds: 2,
    tool_calls: 3,
    elapsed_ms: 12.5,
    steps: [step(1, ['search_leads']), step(2, ['inspect_website', 'draft_email'])],
  }

  const normalised = normaliseRun(detail)

  assert.deepEqual(normalised.tool_sequence, ['search_leads', 'inspect_website', 'draft_email'])
  assert.equal(normalised.did_investigate, true)
  assert.equal(
    normalised.elapsed,
    0.0125,
    'elapsed_ms is milliseconds; treating it as seconds renders 12.5ms as 12.5s'
  )
})

test('a six-second run renders as six seconds, not six thousand', () => {
  const normalised = normaliseRun({ run_id: 'r4', status: 'succeeded', elapsed_ms: 6000, steps: [] })

  assert.equal(normalised.elapsed, 6)
})

test('a seconds-shaped snapshot still passes through', () => {
  // The SSE progress event sends `elapsed` in seconds; no `elapsed_ms` key.
  const normalised = normaliseRun({ run_id: 'r5', status: 'running', elapsed: 3.2 })

  assert.equal(normalised.elapsed, 3.2)
})

test('a run that used no tools says so', () => {
  const normalised = normaliseRun({ run_id: 'r2', status: 'succeeded', steps: [] })

  assert.deepEqual(normalised.tool_sequence, [])
  assert.equal(normalised.did_investigate, false)
  assert.equal(normalised.elapsed, 0)
})

test('a snapshot without steps is tolerated', () => {
  const normalised = normaliseRun({ run_id: 'r3', status: 'running' })

  assert.deepEqual(normalised.steps, [])
  assert.deepEqual(normalised.tool_sequence, [])
})

test('cancelled is terminal, and a stop is not a failure', () => {
  assert.equal(isTerminal('cancelled'), true)
  assert.equal(runOutcome('cancelled'), 'cancelled')
  assert.equal(runOutcome('error'), 'failure')
  assert.equal(runOutcome('stalled'), 'failure')
})

test('a budget stop is not a failure, even when the run "succeeded"', () => {
  assert.equal(runOutcome('truncated'), 'budget')
  assert.equal(runOutcome('succeeded', 'token_budget'), 'budget')
  assert.equal(runOutcome('succeeded', 'final_answer', 2), 'budget')
  assert.equal(runOutcome('succeeded', 'final_answer', 0), 'success')
})

test('an unknown price is a dash, never $0.00', () => {
  assert.equal(formatCost(null), '—')
  assert.equal(formatCost(undefined), '—')
  assert.equal(formatCost({ amount: 0.0017, currency: 'USD' }), '$0.0017')
  assert.equal(formatCost({ amount: 0.5, currency: 'USD' }), '$0.500')
  assert.equal(formatCost({ amount: 0.5, currency: 'CNY', estimated: true }), '~¥0.500')
})

test('token totals compact without pretending to be exact', () => {
  assert.equal(formatTokens(0), '0')
  assert.equal(formatTokens(999), '999')
  assert.equal(formatTokens(1234), '1.2k')
  assert.equal(formatTokens(12400), '12k')
})
