/**
 * Three UI flows that were reported as bugs, exercised in a real browser.
 *
 * Why not a unit test
 * -------------------
 * All three failures lived in the wiring, not in the functions: `patchRow`
 * rolled back correctly while the component restored the whole row; the SSE
 * parser resumed correctly while the page gave up after one error. A DOM
 * environment that stubs the network cannot tell those apart from a passing
 * component -- and the network is exactly what went wrong.
 *
 * What runs here
 * --------------
 * Vite serves the app; every `/api/v1/**` request is intercepted below, so no
 * backend is needed and each scenario decides what the server says. Chromium
 * comes from the Playwright browser cache.
 *
 *   node tests/browser/flows.mjs
 *
 * Env: APP_URL (default http://127.0.0.1:5199), CHROMIUM_PATH (optional),
 *      HEADED=1 to watch it.
 */
import { spawn } from 'node:child_process'
import { existsSync, readdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

const APP_URL = process.env.APP_URL || 'http://127.0.0.1:5199'
const PORT = new URL(APP_URL).port || '5199'

function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH

  const roots = [
    process.env.PLAYWRIGHT_BROWSERS_PATH,
    join(homedir(), 'AppData', 'Local', 'ms-playwright'),
    join(homedir(), '.cache', 'ms-playwright'),
    join(homedir(), 'Library', 'Caches', 'ms-playwright'),
  ].filter(Boolean)

  const candidates = []
  for (const root of roots) {
    if (!existsSync(root)) continue
    for (const entry of readdirSync(root)) {
      if (!entry.startsWith('chromium-')) continue
      candidates.push(join(root, entry, 'chrome-win64', 'chrome.exe'))
      candidates.push(join(root, entry, 'chrome-linux', 'chrome'))
      candidates.push(join(root, entry, 'chrome-mac', 'Chromium.app', 'Contents', 'MacOS', 'Chromium'))
    }
  }
  return candidates.find((path) => existsSync(path)) || ''
}

async function waitForServer(url, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url)
      if (response.ok) return true
    } catch {
      // not up yet
    }
    await delay(300)
  }
  return false
}

function startVite() {
  // ``--host 127.0.0.1`` on purpose: the default binds to ``localhost``, which
  // on Windows resolves to ::1, and a fetch to 127.0.0.1 then never connects --
  // the suite would report "vite did not start" while vite is running fine.
  const child = spawn(
    'npm',
    ['run', 'dev', '--', '--host', '127.0.0.1', '--port', PORT, '--strictPort'],
    { stdio: ['ignore', 'pipe', 'pipe'], shell: process.platform === 'win32' }
  )
  child.log = ''
  const collect = (chunk) => {
    child.log = (child.log + chunk.toString()).slice(-4000)
  }
  child.stdout.on('data', collect)
  child.stderr.on('data', collect)
  return child
}

function stopVite(child) {
  if (!child) return
  if (process.platform === 'win32') {
    // The shell wrapper survives a plain kill() and keeps the port bound.
    spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' })
  } else {
    child.kill()
  }
}

// --- fixtures ---------------------------------------------------------------

const TASK = {
  id: 7,
  title: 'Follow up with ABC Imports',
  description: '',
  task_type: 'follow_up',
  priority: 'high',
  status: 'pending',
  due_date: null,
  customer_id: 3,
}

const CAPABILITIES = {
  configured: true,
  tool_count: 1,
  tools: ['search_prospects'],
  cannot: ['send_email'],
  write_capable: ['create_email_draft'],
  sending: 'human',
}

const RUNNING = {
  run_id: 'run-e2e',
  goal: 'find buyers',
  status: 'running',
  stop_reason: '',
  stop_code: null,
  steps: [],
  rounds: 1,
  tool_calls: 1,
  elapsed_ms: 1200,
  final_output: '',
  did_investigate: true,
}

const DONE = {
  ...RUNNING,
  status: 'succeeded',
  stop_reason: 'final answer',
  stop_code: 'final_answer',
  elapsed_ms: 4200,
  final_output: '结论：测试完成',
  steps: [
    {
      round: 1,
      thought: 'look for buyers',
      tool_calls: [
        { name: 'search_prospects', arguments: { country: 'DE' }, ok: true, observation: '3 found' },
      ],
    },
  ],
}

const json = (body, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

// --- scenarios --------------------------------------------------------------

const results = []

async function scenario(name, fn, browser) {
  const context = await browser.newContext()
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', (err) => errors.push(err.message))

  try {
    await fn(page)
    if (errors.length) throw new Error(`console/page errors: ${errors.join(' | ')}`)
    results.push({ name, ok: true })
    console.log(`  PASS  ${name}`)
  } catch (err) {
    results.push({ name, ok: false, detail: err.message })
    console.log(`  FAIL  ${name}\n        ${err.message}`)
    await page.screenshot({ path: `tests/browser/${name.replace(/\W+/g, '-')}.png` }).catch(() => {})
  } finally {
    await context.close()
  }
}

/** Wait until `check()` is true, or throw with what the page actually said. */
async function until(page, check, { timeout = 15_000, what = 'condition' } = {}) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    if (await check()) return
    await delay(100)
  }
  const text = (await page.locator('body').innerText()).replace(/\s+/g, ' ').slice(0, 300)
  throw new Error(`timed out waiting for ${what}; page says: ${text}`)
}

async function tasksRollback(page) {
  let patchCalls = 0
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (path === '/api/v1/tasks' && request.method() === 'GET') {
      return route.fulfill(json([{ ...TASK }]))
    }
    if (path === '/api/v1/tasks/overdue/list') {
      return route.fulfill(json([]))
    }
    if (path === '/api/v1/tasks/7' && request.method() === 'PUT') {
      patchCalls += 1
      // Slow enough that the optimistic value is observable, then refused.
      await delay(700)
      return route.fulfill(json({ detail: 'the write failed' }, 500))
    }
    return route.fulfill(json({ detail: `unstubbed ${request.method()} ${path}` }, 404))
  })

  await page.goto(`${APP_URL}/tasks`, { waitUntil: 'networkidle' })
  await until(page, async () => (await page.locator('body').innerText()).includes('Follow up with ABC Imports'), {
    what: 'the task row',
  })

  await page.getByRole('button', { name: 'Complete' }).first().click()
  // Optimistic: the badge changes before the request has been answered.
  await until(page, async () => (await page.locator('body').innerText()).includes('completed'), {
    timeout: 500,
    what: 'the optimistic status',
  })

  // Refused: the old value comes back and the row stays.
  await until(
    page,
    async () => {
      const text = await page.locator('body').innerText()
      return text.includes('pending') && !text.includes('completed')
    },
    { what: 'the rolled-back status' }
  )

  if (patchCalls !== 1) throw new Error(`expected one PUT, saw ${patchCalls}`)
}

async function streamFallsBackToPolling(page) {
  let streamCalls = 0
  let pollCalls = 0

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname

    if (path === '/api/v1/agent/capabilities') return route.fulfill(json(CAPABILITIES))
    if (path === '/api/v1/agent/runs') return route.fulfill(json({ runs: [] }))
    if (path === '/api/v1/agent/run' && request.method() === 'POST') {
      return route.fulfill(json({ run_id: 'run-e2e', status: 'running', poll: '/api/v1/agent/runs/run-e2e' }, 202))
    }
    if (path.endsWith('/events')) {
      streamCalls += 1
      // The connection cannot be held: this is the reported symptom.
      return route.abort('connectionfailed')
    }
    if (path === '/api/v1/agent/runs/run-e2e') {
      pollCalls += 1
      return route.fulfill(json(pollCalls >= 2 ? DONE : RUNNING))
    }
    return route.fulfill(json({ detail: `unstubbed ${request.method()} ${path}` }, 404))
  })

  await page.goto(`${APP_URL}/runs`, { waitUntil: 'networkidle' })
  await page.locator('textarea').first().fill('find buyers in Germany')
  await page.getByRole('button', { name: '运行 agent' }).click()

  await until(page, async () => (await page.locator('body').innerText()).includes('结论：测试完成'), {
    timeout: 25_000,
    what: 'the final answer (via polling)',
  })

  if (streamCalls < 3) throw new Error(`expected 3 stream attempts before giving up, saw ${streamCalls}`)
  if (pollCalls < 1) throw new Error('the page never polled after the stream failed')
}

async function qualityPageNeverInventsAZero(page) {
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path !== '/api/v1/analytics/quality') {
      return route.fulfill(json({ detail: `unstubbed ${path}` }, 404))
    }
    return route.fulfill(
      json({
        window: { days: 14, since: '2026-10-07', until: '2026-10-20' },
        generated_at: '2026-10-20T08:00:00',
        runs: {
          total: 4,
          by_status: { succeeded: 2, truncated: 2 },
          success_rate: 50.0,
          elapsed_ms: { p50: 5250.0, p95: 6375.0, max: 6500.0 },
          totals: { rounds: 8, tool_calls: 12, prompt_tokens: 7200, completion_tokens: 540 },
          cached_prompt_ratio: 0.0,
          by_stop_code: [{ value: 'final_answer', count: 2 }],
          by_stop_reason: [],
          by_prompt_version: [{ value: 'abc123', count: 4 }],
        },
        jobs: { total: 0, by_status: {}, success_rate: null, by_job: [], note: 'skipped is not a failure' },
        approvals: { proposals: 0, sent: 0, rejected: 0, pending: 0, acceptance_rate: null, note: '' },
        daily: [],
        closed_loop: {
          available: true,
          sent: 6,
          replied: 2,
          reply_rate: 33.3,
          attributed: 5,
          unattributed: 1,
          coverage: 83.3,
          excluded: { simulated: 0, bounced: 1, failed: 0 },
          by_kind: [{ kind: 'send_follow_up', sent: 4, replied: 2, reply_rate: 50.0 }],
          by_attempt: [{ attempt: '0', sent: 2, replied: 1, reply_rate: 50.0 }],
          note: 'per email, not per customer',
          caveats: ['provenance is recorded at send time'],
        },
      })
    )
  })

  await page.goto(`${APP_URL}/quality`, { waitUntil: 'networkidle' })
  const text = () => page.locator('body').innerText()

  await until(page, async () => (await text()).includes('33.3%'), { what: 'the reply rate' })
  const body = await text()
  for (const expected of ['50%', '83.3%', '6 封', '继续跟进', '退信 1']) {
    if (!body.includes(expected)) throw new Error(`missing ${expected} on the quality page`)
  }

  // A rate with no sample must not render as 0%. Read the value nodes, not the
  // page text: the page's own help line explains the rule ("shows an em dash,
  // not 0%") and a body-wide substring check flags the sentence that states the
  // property it is checking.
  const values = await page.locator('.stat-value, .stat-mini-value').allInnerTexts()
  const zeros = values.filter((value) => value.trim() === '0%')
  if (zeros.length) {
    throw new Error(`a rate with no sample rendered as 0% (values: ${JSON.stringify(values)})`)
  }
  if (!values.some((value) => value.trim() === '—')) {
    throw new Error(`no em dash among the rendered values: ${JSON.stringify(values)}`)
  }
}

async function streamResumesFromCursor(page) {
  const seen = []
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname

    if (path === '/api/v1/agent/capabilities') return route.fulfill(json(CAPABILITIES))
    if (path === '/api/v1/agent/runs') return route.fulfill(json({ runs: [] }))
    if (path === '/api/v1/agent/run' && request.method() === 'POST') {
      return route.fulfill(json({ run_id: 'run-e2e', status: 'running' }, 202))
    }
    if (path.endsWith('/events')) {
      seen.push(url.searchParams.get('after'))
      if (seen.length === 1) {
        // One snapshot, then the connection closes without `done` -- a proxy
        // idle timeout, or a restart. The page must resume, not restart.
        return route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: `id: 1\nevent: snapshot\ndata: ${JSON.stringify(RUNNING)}\n\n`,
        })
      }
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: `id: 2\nevent: done\ndata: ${JSON.stringify(DONE)}\n\n`,
      })
    }
    return route.fulfill(json({ detail: `unstubbed ${request.method()} ${path}` }, 404))
  })

  await page.goto(`${APP_URL}/runs`, { waitUntil: 'networkidle' })
  await page.locator('textarea').first().fill('find buyers in Germany')
  await page.getByRole('button', { name: '运行 agent' }).click()

  await until(page, async () => (await page.locator('body').innerText()).includes('结论：测试完成'), {
    timeout: 25_000,
    what: 'the resumed stream to finish',
  })

  if (seen[0] !== '0') throw new Error(`first connection should start at after=0, was after=${seen[0]}`)
  if (seen[1] !== '1') throw new Error(`the reconnect did not resume from the last round: after=${seen[1]}`)
}

// --- main -------------------------------------------------------------------

async function main() {
  const executablePath = chromiumPath()
  if (!executablePath) {
    console.error('No Chromium found. Set CHROMIUM_PATH, or run: npx playwright install chromium')
    return 3
  }

  let vite = null
  if (!(await waitForServer(`${APP_URL}/`, 1500))) {
    console.log(`starting vite on ${PORT} ...`)
    vite = startVite()
    if (!(await waitForServer(`${APP_URL}/`, 90_000))) {
      console.error('vite did not start. its output was:\n' + (vite.log || '(empty)'))
      stopVite(vite)
      return 3
    }
  }

  const { chromium } = await import('playwright-core')
  const browser = await chromium.launch({ executablePath, headless: !process.env.HEADED })

  console.log(`\nbrowser flows against ${APP_URL}\n`)
  try {
    await scenario('task-completion-rolls-back-on-failure', tasksRollback, browser)
    await scenario('stream-failure-falls-back-to-polling', streamFallsBackToPolling, browser)
    await scenario('stream-resumes-from-the-last-round', streamResumesFromCursor, browser)
    await scenario('quality-page-never-invents-a-zero', qualityPageNeverInventsAZero, browser)
  } finally {
    await browser.close()
    stopVite(vite)
  }

  const failed = results.filter((r) => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} flows passed`)
  return failed.length ? 2 : 0
}

main().then((code) => process.exit(code))
