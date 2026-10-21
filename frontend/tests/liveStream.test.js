/**
 * Live delta merging, without a browser.
 *
 * The rules are small, and each one exists because of a way the stream can
 * misbehave: a delta before its base, a duplicated frame, a reordered frame, a
 * round that moved on. A component test would only prove that the template
 * renders; these prove the transcript cannot be spliced together wrongly.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { applyDelta, applyLive, resetLive } from '../src/utils/liveStream.js'

const turn = () => ({ live: null })

test('a delta before the live frame is dropped, not appended to nothing', () => {
  const t = turn()
  applyDelta(t, { round: 1, kind: 'content', text: 'orphan', seq: 1 })

  assert.equal(t.live, null)
})

test('a live frame establishes the round and its full text', () => {
  const t = turn()
  applyLive(t, { round: 2, content: 'Hello', reasoning: 'thinking', seq: 9 })

  assert.deepEqual(t.live, { round: 2, content: 'Hello', reasoning: 'thinking', seq: 9 })
})

test('a live frame is authoritative and overwrites the buffer', () => {
  const t = turn()
  applyLive(t, { round: 1, content: 'short', reasoning: '', seq: 3 })
  applyLive(t, { round: 1, content: 'the complete text', reasoning: 'r', seq: 12 })

  assert.equal(t.live.content, 'the complete text')
  assert.equal(t.live.seq, 12)
})

test('a late live frame for an older round never rewinds', () => {
  const t = turn()
  applyLive(t, { round: 3, content: 'third', seq: 20 })
  applyLive(t, { round: 2, content: 'second', seq: 10 })

  assert.equal(t.live.round, 3)
  assert.equal(t.live.content, 'third')
})

test('deltas apply only in exact sequence', () => {
  const t = turn()
  applyLive(t, { round: 1, content: '', seq: 0 })

  applyDelta(t, { round: 1, kind: 'content', text: 'a', seq: 1 })
  applyDelta(t, { round: 1, kind: 'content', text: 'b', seq: 2 })
  assert.equal(t.live.content, 'ab')
  assert.equal(t.live.seq, 2)

  // Duplicate.
  applyDelta(t, { round: 1, kind: 'content', text: 'b', seq: 2 })
  assert.equal(t.live.content, 'ab')

  // Gap: wait for the next live/snapshot rather than splicing.
  applyDelta(t, { round: 1, kind: 'content', text: 'd', seq: 4 })
  assert.equal(t.live.content, 'ab')
  assert.equal(t.live.seq, 2)
})

test('reasoning and content are separate channels', () => {
  const t = turn()
  applyLive(t, { round: 1, content: '', reasoning: '', seq: 0 })
  applyDelta(t, { round: 1, kind: 'reasoning', text: 'think', seq: 1 })
  applyDelta(t, { round: 1, kind: 'content', text: 'answer', seq: 2 })

  assert.equal(t.live.reasoning, 'think')
  assert.equal(t.live.content, 'answer')
})

test('a delta for a different round is ignored', () => {
  const t = turn()
  applyLive(t, { round: 2, content: '', reasoning: '', seq: 5 })
  applyDelta(t, { round: 1, kind: 'content', text: 'old', seq: 6 })

  assert.equal(t.live.content, '')
  assert.equal(t.live.seq, 5)
})

test('reset clears the live buffer so a finished turn keeps only its trace', () => {
  const t = turn()
  applyLive(t, { round: 1, content: 'x', seq: 1 })
  resetLive(t)

  assert.equal(t.live, null)
})
