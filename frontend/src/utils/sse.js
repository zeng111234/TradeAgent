/**
 * Server-Sent Events, parsed by hand.
 *
 * `EventSource` would do this and reconnect on its own, but it cannot send
 * headers, it has its own reconnect policy with no jitter and no way to say
 * "give up", and it bypasses the axios instance the rest of the app uses. A
 * `fetch` reader keeps the stream on the same base URL and error path as every
 * other request, and makes the reconnect policy something this code decides
 * instead of something the browser decides silently.
 */

/**
 * Split complete frames off the buffer.
 *
 * A frame ends with a blank line. Whatever is left has not arrived yet, and it
 * is returned rather than parsed: a half-delivered JSON payload is not a
 * shorter payload, and parsing it would either throw or -- worse -- succeed on
 * a truncated string.
 */
export function takeFrames(buffer) {
  const frames = []
  let rest = buffer
  let index = rest.indexOf('\n\n')
  while (index !== -1) {
    const raw = rest.slice(0, index)
    rest = rest.slice(index + 2)
    if (raw.trim()) frames.push(raw)
    index = rest.indexOf('\n\n')
  }
  return { frames, rest }
}

/** One frame into its fields. A comment-only frame is marked as such. */
export function parseFrame(raw) {
  const frame = { event: 'message', id: null, data: '', comment: true }
  for (const rawLine of raw.split('\n')) {
    // Tolerate CRLF: the server writes LF, but a proxy is allowed to rewrite.
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    if (colon === -1) continue
    const field = line.slice(0, colon)
    let value = line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') {
      frame.event = value
      frame.comment = false
    } else if (field === 'id') {
      frame.id = Number(value)
    } else if (field === 'data') {
      frame.data += (frame.data ? '\n' : '') + value
      frame.comment = false
    }
  }
  return frame
}

/**
 * Exponential backoff with jitter, capped.
 *
 * The jitter is not decoration: when a proxy restarts, every open tab loses its
 * stream at the same instant and would otherwise reconnect in lockstep, turning
 * one blip into a burst the server has to absorb.
 */
export function reconnectDelay(attempt, { base = 1000, cap = 30000 } = {}) {
  const step = Math.min(cap, base * 2 ** Math.max(0, attempt - 1))
  return Math.round(step * (0.5 + Math.random() * 0.5))
}

/**
 * Read a `text/event-stream` response until it ends.
 *
 * ``TextDecoder({ stream: true })`` is required, not optional: page text is
 * Chinese, a UTF-8 character can straddle two chunks, and decoding each chunk
 * independently turns one character into two replacement glyphs.
 *
 * Returns whether an unterminated frame was left in the buffer. That is a
 * normal end-of-connection condition (the server was cut off mid-write), not an
 * error -- the reconnect fetches it again from `after=<last id>`.
 */
export async function readEventStream(response, onFrame) {
  if (!response.body) throw new Error('this browser cannot read a response stream')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const { frames, rest } = takeFrames(buffer)
      buffer = rest
      for (const raw of frames) {
        const frame = parseFrame(raw)
        // A heartbeat is a comment: it exists to keep the connection open and
        // carries nothing. Passing it on would make every caller write the
        // same `if (frame.data)` guard.
        if (!frame.comment) onFrame(frame)
      }
    }
  } finally {
    // Releasing the lock lets the connection be aborted from the outside
    // without waiting for this loop to notice.
    reader.releaseLock()
  }

  return { droppedPartial: buffer.trim().length > 0 }
}
