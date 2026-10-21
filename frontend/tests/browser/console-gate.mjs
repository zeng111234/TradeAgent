/**
 * Console gate: the four things the chat UI promises, with no model behind it.
 *
 *   1. A running turn shows its stop button, and clicking it sends the cancel.
 *   2. Live text from `live`/`delta` frames is rendered, and a terminal
 *      `cancelled` frame leaves a neutral note rather than a red error.
 *   3. A restored thread renders reasoning collapsed, tokens, and an unpriced
 *      cost as "unpriced" instead of $0.00.
 *   4. No page errors.
 *
 *   node tests/browser/console-gate.mjs
 *
 * Everything is intercepted; no backend is needed. Env: APP_URL, CHROMIUM_PATH,
 * HEADED=1.
 */
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright-core'

import { APP_URL, chromiumPath, startVite, stopVite, waitForServer } from './harness.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))

const CONVERSATIONS = {
  count: 1,
  conversations: [{
    conversation_id: 'conv-1',
    title: 'which customers need a follow-up?',
    created_at: '2026-10-21T06:00:00',
    updated_at: '2026-10-21T06:05:00',
    run_count: 2,
    last_status: 'succeeded',
    last_goal: 'and their emails?',
    total_tokens: 1500,
  }],
}

const STEP = {
  round: 1,
  thought: '',
  reasoning: 'I should read the book before answering.',
  reasoning_truncated: false,
  latency: 2.1,
  note: '',
  prompt_tokens: 1200,
  cached_prompt_tokens: 480,
  completion_tokens: 300,
  usage_missing: false,
  tool_calls: [{
    name: 'list_customers',
    arguments: { stage: 'all' },
    ok: true,
    observation: '{"customers": []}',
    elapsed: 0.2,
    call_id: 'c1',
    refused_reason: '',
  }],
}

const TURN_ONE = {
  run_id: 'run-2',
  goal: 'which customers need a follow-up?',
  conversation_id: 'conv-1',
  status: 'succeeded',
  stop_code: 'final_answer',
  stop_reason: 'final answer',
  error: null,
  final_output: 'Answer one.',
  rounds: 1,
  tool_calls: 1,
  refused_calls: 0,
  prompt_tokens: 1200,
  cached_prompt_tokens: 480,
  completion_tokens: 300,
  usage_complete: true,
  model: 'fake-model',
  cost: null,
  cache_hit_rate: 0.4,
  elapsed_ms: 3000,
  created_at: '2026-10-21T06:00:00',
  finished_at: '2026-10-21T06:00:03',
  steps: [STEP],
}

const TURN_TWO = {
  ...TURN_ONE,
  run_id: 'run-3',
  goal: 'and their emails?',
  final_output: 'Answer two.',
  steps: [],
  created_at: '2026-10-21T06:04:00',
  finished_at: '2026-10-21T06:05:00',
}

const LIVE_SSE = [
  'event: live',
  'data: {"run_id":"run-1","round":1,"content":"Hel","reasoning":"checking the book","seq":2,"elapsed_ms":400}',
  '',
  'event: delta',
  'data: {"run_id":"run-1","round":1,"kind":"content","text":"lo","seq":3}',
  '',
  'event: done',
  'id: 1',
  `data: ${JSON.stringify({
    run_id: 'run-1',
    goal: 'stream me',
    conversation_id: 'conv-live',
    status: 'cancelled',
    stop_code: 'cancelled',
    stop_reason: 'cancelled by the operator',
    error: null,
    final_output: '',
    rounds: 1,
    tool_calls: 0,
    refused_calls: 0,
    prompt_tokens: 10,
    cached_prompt_tokens: 0,
    completion_tokens: 5,
    usage_complete: true,
    model: 'fake-model',
    cost: null,
    cache_hit_rate: 0,
    elapsed_ms: 900,
    created_at: '2026-10-21T06:06:00',
    finished_at: '2026-10-21T06:06:01',
    steps: [{
      round: 1,
      thought: 'Hello',
      reasoning: 'checking the book',
      reasoning_truncated: false,
      latency: 0.8,
      note: 'cancelled mid-stream',
      prompt_tokens: 10,
      cached_prompt_tokens: 0,
      completion_tokens: 5,
      usage_missing: false,
      tool_calls: [],
    }],
  })}`,
  '',
  '',
].join('\n')

async function main() {
  const vite = startVite()
  if (!(await waitForServer(APP_URL))) {
    console.log(vite.log)
    stopVite(vite)
    return 3
  }

  const browser = await chromium.launch({
    executablePath: await chromiumPath(),
    headless: !process.env.HEADED,
  })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    const text = m.text()
    if (m.type() === 'error' && !/Failed to load resource|net::ERR|404|500/.test(text)) {
      errors.push(text)
    }
  })

  let cancelCalled = false
  let releaseStream
  const streamGate = new Promise((resolve) => { releaseStream = resolve })

  await page.route('**/api/v1/**', async (route) => {
    const url = route.request().url()
    const path = new URL(url).pathname
    const method = route.request().method()
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (url.includes('/agent/capabilities')) {
      return json({
        configured: true, tool_count: 3, tools: [], write_capable: [],
        cannot: [], sending: '',
      })
    }
    if (url.includes('/cancel') && method === 'POST') {
      cancelCalled = true
      return json({ run_id: 'run-1', accepted: true, status: 'cancelling' }, 202)
    }
    if (url.includes('/events')) {
      // Held open until the stop is clicked, so the running state is real.
      await streamGate
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-cache' },
        body: LIVE_SSE,
      })
    }
    if (path.includes('/agent/conversations/')) {
      return json({
        conversation_id: 'conv-1',
        title: CONVERSATIONS.conversations[0].title,
        created_at: '2026-10-21T06:00:00',
        updated_at: '2026-10-21T06:05:00',
        turns: [TURN_ONE, TURN_TWO],
      })
    }
    if (path.endsWith('/agent/conversations')) return json(CONVERSATIONS)
    if (path.endsWith('/agent/run') && method === 'POST') {
      return json({
        run_id: 'run-1',
        status: 'running',
        goal: 'stream me',
        conversation_id: 'conv-live',
        max_seconds: 180,
      })
    }
    return json({})
  })

  try {
    // --- 1. live turn + stop ------------------------------------------------
    await page.goto(`${APP_URL}/console`, { waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.waitForTimeout(500)

    await page.locator('.composer textarea').fill('stream me')
    await page.locator('.composer textarea').press('Enter')
    await page.waitForTimeout(500)

    const stop = page.locator('.composer button', { hasText: /停止|Stop/ })
    if (!(await stop.count())) throw new Error('a running turn has no stop button')

    await stop.first().click()
    await page.waitForTimeout(400)
    if (!cancelCalled) throw new Error('clicking stop did not POST /cancel')

    // Let the held stream deliver its frames: reasoning, two deltas, terminal.
    releaseStream()
    await page.waitForTimeout(900)

    const status = await page.locator('.agent-head .el-tag').first().innerText()
    if (!/已取消|Cancelled/.test(status)) {
      throw new Error(`terminal cancelled status not rendered, got "${status}"`)
    }
    const note = await page.locator('.agent-note').first().innerText()
    if (!/已停止|Stopped/.test(note)) throw new Error(`cancelled note missing: ${note}`)

    // What the model wrote before the stop is kept in the trace, not thrown
    // away with the live buffer. It is one click away, like any finished round.
    await page.locator('.trace-toggle').first().click()
    await page.waitForTimeout(200)
    const kept = await page.locator('.msg-agent .thought, .msg-agent .reasoning-body').allInnerTexts()
    if (!kept.some((text) => text.includes('Hello') || text.includes('checking the book'))) {
      throw new Error(`the partial round was lost after the stop: ${kept.join(' | ')}`)
    }
    if (await page.locator('.composer button', { hasText: /停止|Stop/ }).count()) {
      throw new Error('the stop button survived the terminal frame')
    }

    // --- 2. restored thread: reasoning, tokens, cost, actions --------------
    await page.goto(`${APP_URL}/console?conversation=conv-1`, {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    })
    await page.waitForTimeout(800)

    if ((await page.locator('.rail-item').count()) !== 1) {
      throw new Error('the thread rail did not render the conversation list')
    }
    if ((await page.locator('.rail-item.active').count()) !== 1) {
      throw new Error('the open conversation is not highlighted in the rail')
    }

    const meta = await page.locator('.result-meta').first().innerText()
    // Usage is three separate facts, and the gate checks each one: a single
    // "N tokens" line can be right while the split behind it is wrong.
    for (const wanted of ['fake-model', '输入 1.2k', '输出 300', '缓存命中 40%', '未定价']) {
      if (!meta.includes(wanted)) {
        throw new Error(`result meta missing "${wanted}": ${meta.replace(/\n/g, ' ')}`)
      }
    }

    // The trace is collapsed by default; reasoning lives inside it.
    await page.locator('.trace-toggle').first().click()
    await page.waitForTimeout(250)
    const reasoning = page.locator('.reasoning').first()
    if (!(await reasoning.count())) throw new Error('reasoning block did not render')
    // `getAttribute('open')` returns "" when present, which is falsy -- read
    // the property instead, or this check silently inverts.
    if (await reasoning.evaluate((el) => el.open)) {
      throw new Error('reasoning is expanded by default; it should be evidence on demand')
    }
    await reasoning.locator('summary').click()
    await page.waitForTimeout(200)
    if (!(await reasoning.evaluate((el) => el.open))) {
      throw new Error('reasoning did not expand')
    }
    const body = await reasoning.locator('.reasoning-body').innerText()
    if (!body.includes('read the book')) throw new Error(`reasoning text wrong: ${body}`)

    const actions = await page.locator('.turn').first().locator('.result-actions button').count()
    if (actions !== 3) throw new Error(`expected copy/rerun/export, got ${actions} actions`)

    // The rail must sit at the same gutter as the topbar breadcrumb. It used to
    // be centred inside a wider grid, which left 96px of empty page between the
    // dark sidebar and the conversation list at 1600px: a blank block that read
    // as a broken column, not as a margin. 1440 has no slack to give away, so
    // widen first -- at that width centring and starting look identical.
    await page.setViewportSize({ width: 1600, height: 900 })
    await page.waitForTimeout(250)
    const frame = await page.evaluate(() => {
      const box = (sel) => {
        const el = document.querySelector(sel)
        return el ? el.getBoundingClientRect() : null
      }
      const rail = box('.thread-rail')
      const sidebar = box('.sidebar')
      const crumb = box('.topbar-crumb')
      return {
        gap: Math.round(rail.x - sidebar.right),
        railX: Math.round(rail.x),
        crumbX: Math.round(crumb.x),
      }
    })
    if (frame.gap > 40) {
      throw new Error(`dead band between sidebar and rail: ${JSON.stringify(frame)}`)
    }
    if (Math.abs(frame.railX - frame.crumbX) > 2) {
      throw new Error(`rail is not aligned with the breadcrumb: ${JSON.stringify(frame)}`)
    }

    // A long transcript scrolls inside the console, not the document. Checked
    // further down at 1000px, where the single-column layout has no stretched
    // row to hide the problem: at 1440 the row is simply taller than the
    // content, so a broken row and a working one measure the same.

    // Below 1280 the thread list is a slide-over. It used to slide out at
    // left: 0, i.e. underneath the sticky sidebar (z-index 100), so 220 of its
    // 272 pixels were covered: the list looked like a black strip and its rows
    // could not be clicked. Resize the same page -- the routes stay installed.
    await page.setViewportSize({ width: 1000, height: 800 })
    await page.reload({ waitUntil: 'networkidle' })

    // A long transcript scrolls inside the console, not the document. With an
    // auto-sized grid row the page outgrew the viewport, the body scrolled, the
    // sticky sidebar slid up and left a bare strip under itself, and the
    // composer ended up below the fold. A short window reproduces it with the
    // stubbed answer, without needing a long one.
    await page.setViewportSize({ width: 1000, height: 460 })
    await page.waitForTimeout(300)
    const tall = await page.evaluate(() => {
      const scroller = document.querySelector('.console-scroll')
      const shell = document.querySelector('.console-shell').getBoundingClientRect()
      const pageBox = document.querySelector('.console-page').getBoundingClientRect()
      return {
        pageHeight: Math.round(pageBox.height),
        shellHeight: Math.round(shell.height),
        docScroll: document.documentElement.scrollHeight,
        viewport: document.documentElement.clientHeight,
        innerScrolls: scroller.scrollHeight > scroller.clientHeight + 4,
        composerBottom: Math.round(document.querySelector('.composer').getBoundingClientRect().bottom),
        sidebarBottom: Math.round(document.querySelector('.sidebar').getBoundingClientRect().bottom),
      }
    })
    if (tall.pageHeight > tall.shellHeight + 1) {
      throw new Error(`the page grew past its shell: ${JSON.stringify(tall)}`)
    }
    if (tall.docScroll > tall.viewport + 1) {
      throw new Error(`the document grew past the viewport: ${JSON.stringify(tall)}`)
    }
    if (!tall.innerScrolls) {
      throw new Error(`the transcript is not the scroller: ${JSON.stringify(tall)}`)
    }
    if (tall.composerBottom > tall.viewport + 1 || tall.sidebarBottom > tall.viewport + 1) {
      throw new Error(`composer or sidebar pushed out of view: ${JSON.stringify(tall)}`)
    }
    await page.setViewportSize({ width: 1000, height: 800 })
    await page.waitForTimeout(250)

    await page.locator('.rail-toggle').click()
    await page.waitForTimeout(300)
    const drawer = await page.evaluate(() => {
      const rail = document.querySelector('.thread-rail')
      const sidebar = document.querySelector('.sidebar')
      const item = document.querySelector('.rail-item')
      if (!rail || !sidebar || !item) return null
      const r = item.getBoundingClientRect()
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
      return {
        railX: Math.round(rail.getBoundingClientRect().x),
        sidebarRight: Math.round(sidebar.getBoundingClientRect().right),
        itemClickable: Boolean(hit && hit.closest('.rail-item')),
      }
    })
    if (!drawer) throw new Error('thread drawer did not render at 1000px')
    if (drawer.railX < drawer.sidebarRight - 1) {
      throw new Error(`drawer starts under the sidebar: ${JSON.stringify(drawer)}`)
    }
    if (!drawer.itemClickable) {
      throw new Error(`drawer rows are covered: ${JSON.stringify(drawer)}`)
    }

    if (errors.length) throw new Error(`page errors: ${errors.slice(0, 3).join(' | ')}`)

    console.log('PASS console-gate: stop+cancel, live text, cancelled note, reasoning, tokens, unpriced cost, rail alignment, bounded page, drawer clears sidebar')
    return 0
  } catch (e) {
    console.log(`FAIL console-gate: ${e.message}`)
    try {
      await page.screenshot({
        path: join(HERE, '..', '..', '..', 'artifacts', 'ui', 'console-gate-fail.png'),
      })
    } catch { /* best effort */ }
    return 1
  } finally {
    await browser.close()
    stopVite(vite)
  }
}

process.exit(await main())
