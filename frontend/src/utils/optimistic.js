/**
 * Optimistic writes, and the only kind of rollback that is safe.
 *
 * The views in this app were all pessimistic: await the request, then refetch
 * the list. That is the right default -- a refetch *is* a rollback, because it
 * replaces whatever the browser believed with what the server actually stored.
 * These helpers exist for the few rows where waiting for a round trip is
 * visible to the user (4-6 ms locally, 100-600 ms on a phone), and the rule they
 * encode is the part that is easy to get wrong:
 *
 * **A rollback must undo the fields this call wrote, and only while this call is
 * still the last thing written to that row.**
 *
 * Not "restore the row from a snapshot". A row can have two independent edits in
 * flight -- mark it completed, then change its priority, and only the first one
 * fails. Restoring the snapshot taken before the completion would silently undo
 * the priority change that *succeeded*. Not "assign the whole array back" for
 * the same reason, one level up.
 *
 * And not unconditionally, either: if a later edit already replaced this row's
 * value, the failed call's rollback is stale -- writing it would resurrect a
 * value the user has since moved on from. In that case `undo()` reports false
 * and the caller has to refetch, because only the server knows what the row is
 * now.
 *
 * Row identity is the index key here (`id`), so `reconcile` merges the server's
 * version *into* the existing object instead of replacing it. Replacing it would
 * drop the generation marker and make every later rollback look stale.
 */

/** Monotonic per-row counter. WeakMap, so rows can be garbage collected. */
const generations = new WeakMap()

/**
 * Last server-confirmed value per field, per row.
 *
 * Not "the value this patch found": two optimistic writes to the same field
 * would then chain, and rolling back the second one would restore the first
 * *optimistic* value -- a number the server never accepted. If both requests
 * then failed, the row would sit on a value nobody confirmed, with both undos
 * refusing to run. The baseline is the last value the server actually returned.
 */
const baselines = new WeakMap()

function bump(row) {
  const next = (generations.get(row) || 0) + 1
  generations.set(row, next)
  return next
}

function generationOf(row) {
  return generations.get(row) || 0
}

function baselineFor(row) {
  let fields = baselines.get(row)
  if (!fields) {
    fields = new Map()
    baselines.set(row, fields)
  }
  return fields
}

function findRow(rows, id) {
  return rows.find((row) => row.id === id)
}

/**
 * Apply `patch` to the row with `id` and return its undo.
 *
 * `undo()` writes back only the keys in `patch`, restoring each to the last
 * value the server confirmed, and returns false (doing nothing) if a newer
 * write has landed on the row since. The caller's failure path must therefore
 * be:
 *
 *     if (!undo()) await reload()
 *
 * returning silently would leave the row showing a value the server rejected.
 */
export function patchRow(rows, id, patch) {
  const row = findRow(rows, id)
  if (!row) {
    return { found: false, undo: () => false }
  }

  const confirmed = baselineFor(row)
  for (const key of Object.keys(patch)) {
    // Only on the first optimistic write to this field: later writes must not
    // overwrite the baseline with an optimistic value.
    if (!confirmed.has(key)) confirmed.set(key, row[key])
  }

  Object.assign(row, patch)
  const generation = bump(row)

  return {
    found: true,
    index: rows.indexOf(row),
    undo() {
      if (generationOf(row) !== generation) return false
      for (const key of Object.keys(patch)) {
        if (confirmed.has(key)) row[key] = confirmed.get(key)
        confirmed.delete(key)
      }
      // Invalidate this undo and any older one; a newer write owns the row now.
      bump(row)
      return true
    },
  }
}

/**
 * Remove a row optimistically, remembering where it was.
 *
 * The position matters: re-adding a failed delete at the end of the list makes
 * the row look like it moved, and in a list sorted by due date that is a lie the
 * user can see. The anchor is the id of the row that followed it, not its index
 * -- another row removed while the request was in flight would shift the index,
 * and the restored row would land in the wrong place.
 */
export function removeRow(rows, id) {
  const index = rows.findIndex((row) => row.id === id)
  if (index === -1) {
    return { removed: false, undo: () => false }
  }

  const [row] = rows.splice(index, 1)
  const anchor = rows[index]?.id ?? null
  let restored = false

  return {
    removed: true,
    row,
    index,
    undo() {
      if (restored) return true
      restored = true
      const at = anchor === null ? rows.length : rows.findIndex((r) => r.id === anchor)
      rows.splice(at === -1 ? rows.length : at, 0, row)
      return true
    },
  }
}

/**
 * Merge the server's copy of a row into the local one.
 *
 * Called on success, before the optimistic value is considered confirmed: the
 * server may have filled in fields the client could not predict (`completed_at`,
 * `updated_at`). Merging in place, not `rows[i] = serverRow`, keeps the row's
 * identity -- and therefore its generation marker -- intact.
 */
export function reconcile(rows, serverRow) {
  const row = findRow(rows, serverRow.id)
  if (!row) return false
  Object.assign(row, serverRow)
  // These fields are now confirmed: the next optimistic write on them has to
  // record a fresh baseline from what the server just said.
  const confirmed = baselineFor(row)
  for (const key of Object.keys(serverRow)) confirmed.delete(key)
  // The server has now spoken for this row. An undo created before this point
  // describes a state that no longer exists, so it must not be able to write
  // the optimistic value back over the authoritative one.
  bump(row)
  return true
}

/** The message an API failure should show: the server's, else a fallback. */
export function failureDetail(error, fallback) {
  return (
    error?.response?.data?.detail ||
    error?.response?.data?.message ||
    error?.message ||
    fallback
  )
}
