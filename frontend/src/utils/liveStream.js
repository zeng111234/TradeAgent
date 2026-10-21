/**
 * Merge live delta frames into a turn.
 *
 * Pure and separate from the component because the rules are the part with a
 * decision in them, and each rule exists because of a concrete failure:
 *
 * - a `delta` that arrives before its `live` frame has no base to append to, so
 *   it is dropped and the next `live` (which carries the whole round) repairs
 *   it;
 * - a duplicated or reordered frame must not append text twice, so `seq` has to
 *   be exactly one greater than the last one applied;
 * - a `live` frame is authoritative and overwrites the local buffer, which is
 *   what makes a reconnect cheap: one frame instead of replaying a round.
 */

/** Start or resynchronise the live round from an authoritative frame. */
export function applyLive(turn, payload) {
  const round = Number(payload?.round) || 0
  if (!round) return turn
  if (turn.live && round < turn.live.round) return turn
  turn.live = {
    round,
    content: payload.content || '',
    reasoning: payload.reasoning || '',
    seq: Number(payload.seq) || 0,
  }
  return turn
}

/** Append one best-effort increment. Anything out of order is ignored. */
export function applyDelta(turn, payload) {
  const live = turn.live
  if (!live) return turn
  if (Number(payload?.round) !== live.round) return turn
  const seq = Number(payload?.seq)
  if (!Number.isFinite(seq) || seq <= live.seq) return turn
  if (seq !== live.seq + 1) {
    // A gap: the ring buffer dropped frames, or a proxy reordered them. Waiting
    // for the next `live`/`snapshot` is correct; guessing would splice the tail
    // of one sentence onto the head of another.
    return turn
  }
  const text = payload.text || ''
  if (payload.kind === 'reasoning') live.reasoning += text
  else live.content += text
  live.seq = seq
  return turn
}

export function resetLive(turn) {
  turn.live = null
  return turn
}
