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
  assert.equal(normalised.elapsed, 12.5, 'elapsed_ms holds seconds, not milliseconds')
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
