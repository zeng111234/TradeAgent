/**
 * The stream parser, run against byte chunks instead of a server.
 *
 * Every case here is a real failure mode of SSE over a network that is not
 * cooperating: a frame split across two TCP reads, a heartbeat in the middle of
 * the stream, a connection that dies mid-frame, and a Chinese character cut in
 * half by the transport.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  parseFrame,
  readEventStream,
  reconnectDelay,
  takeFrames,
} from '../src/utils/sse.js'

const encoder = new TextEncoder()

function streamOf(chunks) {
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) {
          controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk)
        }
        controller.close()
      },
    })
  )
}

async function collect(chunks) {
  const frames = []
  const result = await readEventStream(streamOf(chunks), (frame) => frames.push(frame))
  return { frames, ...result }
}

test('two complete frames and one partial are split correctly', () => {
  const { frames, rest } = takeFrames('event: a\ndata: 1\n\nevent: b\ndata: 2\n\npartial')

  assert.equal(frames.length, 2)
  assert.equal(rest, 'partial')
})

test('a buffer with no frame boundary yields nothing and keeps everything', () => {
  const { frames, rest } = takeFrames('event: a\ndata: 1')

  assert.deepEqual(frames, [])
  assert.equal(rest, 'event: a\ndata: 1')
})

test('a frame carries its event name, id and payload', () => {
  const frame = parseFrame('id: 7\nevent: done\ndata: {"ok":true}')

  assert.equal(frame.event, 'done')
  assert.equal(frame.id, 7)
  assert.deepEqual(JSON.parse(frame.data), { ok: true })
  assert.equal(frame.comment, false)
})

test('a heartbeat is a comment, not an event', () => {
  const frame = parseFrame(': heartbeat')

  assert.equal(frame.comment, true)
  assert.equal(frame.data, '')
})

test('CRLF is tolerated', () => {
  const frame = parseFrame('event: snapshot\r\ndata: {"a":1}\r')

  assert.equal(frame.event, 'snapshot')
  assert.deepEqual(JSON.parse(frame.data), { a: 1 })
})

test('multiple data lines are joined with newlines', () => {
  const frame = parseFrame('data: line one\ndata: line two')

  assert.equal(frame.data, 'line one\nline two')
})

test('a frame delivered in two chunks is parsed once, whole', async () => {
  const { frames } = await collect([
    'event: snapshot\ndata: {"rou',
    'nds":2}\n\n',
  ])

  assert.equal(frames.length, 1)
  assert.deepEqual(JSON.parse(frames[0].data), { rounds: 2 })
})

test('a Chinese character split across chunks is not mangled', async () => {
  const payload = 'event: snapshot\ndata: {"note":"报价"}\n\n'
  const bytes = encoder.encode(payload)
  // Cut immediately after the first byte of the multi-byte character.
  const cut = bytes.indexOf(0xe6) + 1
  assert.notEqual(cut, 0, 'the test string should contain a multi-byte character')

  const { frames } = await collect([bytes.slice(0, cut), bytes.slice(cut)])

  assert.deepEqual(JSON.parse(frames[0].data), { note: '报价' })
})

test('heartbeats never reach the caller', async () => {
  const { frames } = await collect([
    ': heartbeat\n\n',
    'event: snapshot\ndata: {"rounds":1}\n\n',
    ': heartbeat\n\n',
  ])

  assert.equal(frames.length, 1)
  assert.equal(frames[0].event, 'snapshot')
})

test('a connection that dies mid-frame reports the partial instead of parsing it', async () => {
  const { frames, droppedPartial } = await collect([
    'event: snapshot\ndata: {"rounds":1}\n\nevent: snapshot\ndata: {"rou',
  ])

  assert.equal(frames.length, 1, 'the incomplete frame must not be delivered')
  assert.equal(droppedPartial, true)
})

test('a response without a body fails loudly', async () => {
  await assert.rejects(
    () => readEventStream(new Response(null), () => {}),
    /cannot read a response stream/
  )
})

test('reconnect delays grow, stay capped, and are jittered', () => {
  const delays = [1, 2, 3, 4, 5, 6, 10].map((attempt) => reconnectDelay(attempt))

  for (let index = 1; index < delays.length; index += 1) {
    assert.ok(delays[index] >= delays[index - 1] * 0.5, `delay ${index} went backwards`)
  }
  assert.ok(Math.max(...delays) <= 30000, 'cap is not applied')

  // Jitter means the same attempt does not always produce the same number.
  const samples = new Set(Array.from({ length: 20 }, () => reconnectDelay(3)))
  assert.ok(samples.size > 1, 'no jitter: every tab reconnects in lockstep')
})

test('a real HTTP stream is consumed as it is written, not when it closes', async () => {
  const { createServer } = await import('node:http')
  const arrivals = []
  const server = createServer((request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/event-stream' })
    response.write('event: snapshot\ndata: {"rounds":1}\n\n')
    setTimeout(() => {
      response.write(': heartbeat\n\n')
      response.write('event: done\ndata: {"rounds":2}\n\n')
      response.end()
    }, 40)
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()

  try {
    const response = await fetch(`http://127.0.0.1:${port}/events`)
    const { droppedPartial } = await readEventStream(response, (frame) => {
      arrivals.push({ event: frame.event, at: Date.now() })
    })

    assert.deepEqual(arrivals.map((entry) => entry.event), ['snapshot', 'done'])
    assert.ok(
      arrivals[1].at - arrivals[0].at >= 20,
      'both events arrived together: the stream was buffered'
    )
    assert.equal(droppedPartial, false)
  } finally {
    server.close()
  }
})
