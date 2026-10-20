/**
 * Gate for the Quality rearrangement. The page's thesis is that a rate with
 * no sample shows an em dash, not 0% -- so the gate re-reads the sentence that
 * says so and checks it survived verbatim, then checks the two interactions:
 * switching the time window, and a failed refresh keeping the old report on
 * screen (an empty window reads as "nothing ran").
 */
import { chromiumPath, startVite, stopVite, APP_URL, waitForServer } from './harness.mjs'
import { chromium } from 'playwright-core'

const REPORT = (days) => ({
  window: { since: '2026-10-06', until: '2026-10-20', days },
  generated_at: '2026-10-20T12:00:00Z',
  runs: {
    total: 8,
    by_status: { succeeded: 6, error: 1, running: 1 },
    success_rate: 85.7,
    elapsed_ms: { p50: 4000, p95: 12000, max: 15000 },
    by_stop_code: [{ value: 'finished', count: 6 }],
    by_prompt_version: [{ value: 'abc123', count: 8 }],
    totals: { tool_calls: 20, prompt_tokens: 1000, completion_tokens: 400 },
  },
  jobs: {
    total: 10,
    success_rate: 90.0,
    note: '跳过不算失败',
    by_job: [{ job_id: 'daily', total: 10, succeeded: 9, failed: 0, skipped: 1, abandoned: 0 }],
  },
  approvals: { sent: 4, rejected: 1, pending: 2, acceptance_rate: 80.0 },
  closed_loop: {
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
  daily: [{ date: '2026-10-19', runs_total: 1, runs_succeeded: 1, runs_failed: 0, runs_truncated: 0, jobs_total: 1, jobs_failed: 0 }],
})

const THESIS =
  '没有样本的比例显示 — 而不是 0% —— 两者是不同的结论。'

async function main() {
  const vite = startVite()
  if (!(await waitForServer(`${APP_URL}/`, 90_000))) {
    console.log(vite.log)
    stopVite(vite)
    return 3
  }
  const browser = await chromium.launch({ executablePath: chromiumPath(), headless: !process.env.HEADED })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  // A deliberately failed refresh logs a resource error and the API layer logs
  // the transport failure. Both are the behaviour under test, not page bugs.
  const isExpectedNoise = (text) =>
    /Failed to load resource/.test(text) || /API Error: transport/.test(text)
  page.on('console', (m) => {
    if (m.type() === 'error' && !isExpectedNoise(m.text())) errors.push(m.text())
  })
  page.on('pageerror', (e) => {
    if (!isExpectedNoise(e.message)) errors.push(`pageerror: ${e.message}`)
  })

  let requestedDays = 14
  let failNext = false
  try {
    await page.route('**/api/v1/analytics/quality*', (route) => {
      if (failNext) {
        failNext = false
        return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'boom' }) })
      }
      const url = new URL(route.request().url())
      requestedDays = Number(url.searchParams.get('days') || 14)
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(REPORT(requestedDays)) })
    })

    await page.goto(`${APP_URL}/quality`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
    await page.waitForTimeout(900)

    // 1. The thesis sentence must survive verbatim -- it is the page's contract
    //    with the reader, and the layout pass must not "tidy" it away.
    const body = await page.locator('body').innerText()
    if (!body.includes(THESIS)) throw new Error('the em-dash rule sentence is missing or reworded')

    // 2. Four stat cards, each label allowed to wrap inside its track.
    const cards = await page.locator('.stat-grid .stat-card').count()
    if (cards !== 4) throw new Error(`expected 4 stat cards, got ${cards}`)

    // 3. Window switch asks the API for the new window and re-renders.
    await page.locator('.el-radio-button', { hasText: '7 天' }).click()
    await page.waitForTimeout(600)
    if (requestedDays !== 7) throw new Error(`window switch did not request days=7 (got ${requestedDays})`)

    // 4. A failed refresh keeps the last report on screen and says why. An
    //    empty page here reads as "nothing ran", which is a different claim.
    failNext = true
    await page.locator('button', { hasText: '刷新' }).click()
    await page.waitForTimeout(700)
    const afterFail = await page.locator('body').innerText()
    if (!afterFail.includes('读不到质量指标')) throw new Error('failed refresh did not report the error')
    if (!afterFail.includes('85.7%')) throw new Error('failed refresh dropped the last report')
    if (await page.locator('.el-skeleton').count()) throw new Error('failed refresh fell back to a skeleton')

    if (errors.length) throw new Error(`console errors: ${JSON.stringify(errors.slice(0, 3))}`)
    console.log('PASS quality-gate: thesis intact, 4 cards, window switch ok, failed refresh keeps report')
  } catch (err) {
    console.log(`FAIL quality-gate: ${err.message}`)
    await page.screenshot({ path: 'tests/browser/quality-gate-fail.png', fullPage: true }).catch(() => {})
    return 1
  } finally {
    await browser.close()
    stopVite(vite)
  }
  return 0
}

process.exit(await main())
