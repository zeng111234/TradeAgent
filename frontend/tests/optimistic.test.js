/**
 * The rollback rules, run without a browser.
 *
 * Every test here is a specific way an optimistic UI goes wrong in production.
 * The important one is `only the fields this call wrote are restored`: that is
 * the difference between a rollback and "put the old row back", and the second
 * one silently discards edits that succeeded.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  failureDetail,
  patchRow,
  reconcile,
  removeRow,
} from '../src/utils/optimistic.js'

const rows = () => [
  { id: 1, title: 'call the buyer', status: 'pending', priority: 'medium' },
  { id: 2, title: 'send the quote', status: 'pending', priority: 'high' },
  { id: 3, title: 'book the sample', status: 'pending', priority: 'low' },
]

test('a patch is visible before the request finishes', () => {
  const list = rows()
  patchRow(list, 2, { status: 'completed' })

  assert.equal(list[1].status, 'completed')
})

test('undo puts back exactly the value that was there', () => {
  const list = rows()
  const { undo } = patchRow(list, 2, { status: 'completed' })

  assert.equal(undo(), true)
  assert.equal(list[1].status, 'pending')
  assert.equal(list[1].priority, 'high', 'fields outside the patch were touched')
})

test('only the fields this call wrote are restored', () => {
  const list = rows()
  const completed = patchRow(list, 1, { status: 'completed' })

  // A second, independent write lands on the same row while the first request
  // is still in flight. It might be another optimistic action, or a poller that
  // refreshed the row -- either way it succeeded.
  list[0].priority = 'urgent'

  assert.equal(completed.undo(), true)
  assert.equal(list[0].status, 'pending')
  assert.equal(
    list[0].priority,
    'urgent',
    'rolling back from a whole-row snapshot would have undone the priority change too'
  )
})

test('an undo that has been overtaken does nothing and says so', () => {
  const list = rows()
  const first = patchRow(list, 1, { status: 'completed' })
  const second = patchRow(list, 1, { status: 'cancelled' })

  assert.equal(first.undo(), false, 'a stale undo must report that it was skipped')
  assert.equal(list[0].status, 'cancelled', 'the newer value was overwritten')

  assert.equal(second.undo(), true)
  assert.equal(list[0].status, 'pending')
})

test('a server confirmation wins over an earlier undo', () => {
  const list = rows()
  const pending = patchRow(list, 1, { status: 'completed' })

  reconcile(list, { id: 1, status: 'completed', priority: 'medium', completed_at: '2026-10-19T12:00:00' })

  assert.equal(list[0].completed_at, '2026-10-19T12:00:00')
  assert.equal(
    pending.undo(),
    false,
    'an undo created before the server answered must not write over the answer'
  )
  assert.equal(list[0].status, 'completed')
})

test('reconcile keeps the row object, so identity-based tracking survives it', () => {
  const list = rows()
  const before = list[0]
  const patched = patchRow(list, 1, { status: 'completed' })

  reconcile(list, { id: 1, status: 'completed' })

  assert.equal(list[0], before, 'the row was replaced instead of merged into')
  assert.equal(patched.undo(), false, 'the replacement lost the generation marker')
})

test('a failed delete comes back where it was, not at the end', () => {
  const list = rows()
  const { undo } = removeRow(list, 2)

  assert.deepEqual(list.map((row) => row.id), [1, 3])
  // Something else was removed while the request was in flight.
  removeRow(list, 1)

  undo()

  assert.deepEqual(list.map((row) => row.id), [2, 3], 'the row was appended instead of restored')
})

test('removing a row that was never there is a no-op that says so', () => {
  const list = rows()
  const { removed, undo } = removeRow(list, 99)

  assert.equal(removed, false)
  assert.equal(undo(), false)
  assert.equal(list.length, 3)
})

test('patching a row that is no longer in the list does not invent it', () => {
  const list = rows()
  const { found, undo } = patchRow(list, 99, { status: 'completed' })

  assert.equal(found, false)
  assert.equal(undo(), false)
  assert.equal(list.length, 3)
})

test('the failure message prefers the server over the generic fallback', () => {
  assert.equal(
    failureDetail({ response: { data: { detail: 'this task was already completed' } } }, 'Failed'),
    'this task was already completed'
  )
  assert.equal(failureDetail({ message: 'Network Error' }, 'Failed'), 'Network Error')
  assert.equal(failureDetail(null, 'Failed'), 'Failed')
})
