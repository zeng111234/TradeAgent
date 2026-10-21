/**
 * Gate for the Runs rearrangement: form is the first card, the history table
 * keeps one row height at 48px or under, the tool path shows two tools plus a
 * count with the whole sequence on hover, and a row click opens the drawer.
 */
import { chromiumPath, startVite, stopVite, stubRemainingApi, APP_URL, waitForServer } from './harness.mjs'
import { chromium } from 'playwright-core'

const RUNS = [
  {
    run_id: 'run-gate-0001',
    status: 'succeeded',
    goal: '看看我现有客户里哪些需要今天跟进，给我理由',
    rounds: 3,
    tool_calls: 7,
    tool_sequence: ['list_customers', 'get_customer', 'list_tasks', 'search_knowledge', 'get_task', 'list_email_logs', 'get_analytics'],
    stop_reason: 'finished',
    did_investigate: true,
    elapsed: 12.5,
    prompt_tokens: 1000,
    completion_tokens: 400,
    final_output: '结论：测试完成',
    mermaid_sequence: 'sequenceDiagram\n  A->>B: hi',
    steps: [],
    created_at: '2026-10-20T10:00:00Z',
  },
  {
    run_id: 'run-gate-0002',
    status: 'error',
    goal: 'short goal',
    rounds: 1,
    tool_calls: 1,
    tool_sequence: ['list_customers'],
    stop_reason: 'tool error',
    did_investigate: false,
    elapsed: 2,
    prompt_tokens: 10,
    completion_tokens: 5,
    final_output: '',
    mermaid_sequence: '',
    steps: [],
    created_at: '2026-10-20T11:00:00Z',
  },
]

const CAPABILITIES = {
  configured: true,
  tool_count: 11,
  write_capable: ['create_email_draft', 'upsert_customer'],
  cannot: [
    'send email to a customer',
    'delete or modify an existing customer',
    'delete or modify an email log',
    'quote a price from a price list or a live freight rate: there is none',
    'promise a ship date: there is no schedule or carrier integration',
    'export contacts in bulk: the list view omits contact details, and each customer is a separate lookup',
  ],
  sending: "Drafts are created with status 'pending'. A human sends them through the existing review flow.",
}

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
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))

  try {
    // Registered first so the two stubs below win. Harmless today -- this
    // gate's pages are fully stubbed -- but it keeps the next request someone
    // adds from becoming a CI-only 500.
    await stubRemainingApi(page)
    await page.route('**/api/v1/agent/capabilities', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CAPABILITIES) }))
    // One dispatcher, because the list call carries query params and the glob
    // `**/agent/runs` would not match `**/agent/runs?status=…`.
    await page.route('**/api/v1/agent/runs**', (route) => {
      const path = new URL(route.request().url()).pathname
      const match = path.match(/\/runs\/([^/]+)$/)
      if (match && route.request().method() === 'GET') {
        const run = RUNS.find((x) => x.run_id === decodeURIComponent(match[1]))
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(run || RUNS[0]) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ runs: RUNS }) })
    })

    await page.goto(`${APP_URL}/runs`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
    await page.waitForTimeout(1200)

    // 1. The form card is first: the page's only action is not below the docs.
    const firstCardTitle = await page.locator('.el-card .card-title').first().innerText()
    if (!firstCardTitle.includes('运行')) {
      throw new Error(`run form should be the first card, found: ${firstCardTitle}`)
    }

    // 2. One row height, at or under 48px. Was 33/49/109/149: the tool path
    //    wrapped to as many lines as it had tools.
    const heights = await page.locator('.el-table__body tr.el-table__row').evaluateAll((rows) =>
      rows.map((r) => Math.round(r.getBoundingClientRect().height)))
    const unique = [...new Set(heights)]
    if (unique.length !== 1) throw new Error(`row heights not uniform: ${JSON.stringify(unique)}`)
    if (unique[0] > 48) throw new Error(`row height ${unique[0]} exceeds 48px`)

    // 3. Tool path: first two tools + "+N", whole sequence on hover. The
    //    "+N" must be *visibly* inside the cell: innerText includes clipped
    //    overflow, so a text check passes while the count is cut off.
    const cell = page.locator('.el-table__row').first().locator('.tool-path-cell')
    const cellText = (await cell.innerText()).replace(/\s+/g, ' ')
    if (!cellText.includes('list_customers') || !cellText.includes('get_customer')) {
      throw new Error(`first two tools missing: ${cellText}`)
    }
    if (!cellText.includes('+5')) throw new Error(`overflow count missing: ${cellText}`)
    if (cellText.includes('list_tasks')) throw new Error(`third tool should be behind the count: ${cellText}`)
    const clipped = await cell.evaluate((el) => {
      const box = el.getBoundingClientRect()
      const more = el.querySelector('.tool-path-more')
      if (!more) return 'no .tool-path-more element'
      const r = more.getBoundingClientRect()
      return r.width > 0 && r.right <= box.right + 1 && r.left >= box.left - 1
        ? false
        : `+N clipped: cell [${box.left.toFixed(0)},${box.right.toFixed(0)}] more [${r.left.toFixed(0)},${r.right.toFixed(0)}]`
    })
    if (clipped) throw new Error(clipped)
    await cell.hover()
    await page.waitForTimeout(400)
    const tip = await page.locator('.el-popper:visible').innerText().catch(() => '')
    if (!tip.includes('list_email_logs')) throw new Error(`hover should show the full path: ${tip}`)

    // 4. Row click opens the drawer with the run's conclusion.
    await page.locator('.el-table__row').first().click()
    await page.waitForTimeout(600)
    const drawer = await page.locator('.el-drawer:visible').count()
    if (!drawer) throw new Error('row click did not open the drawer')
    const drawerText = await page.locator('.el-drawer:visible').innerText()
    if (!drawerText.includes('结论：测试完成')) throw new Error('drawer is missing the run output')

    // 5. Boundary is a callout now, and the deliberately-absent tools are not
    //    painted as errors.
    const callout = await page.locator('.callout').count()
    if (!callout) throw new Error('boundary callout missing')
    const dangerTags = await page.locator('.callout .el-tag--danger').count()
    if (dangerTags) throw new Error(`boundary tags should not be danger: ${dangerTags}`)
    const pageWidth = await page.evaluate(() => [document.scrollingElement.scrollWidth, window.innerWidth])
    if (pageWidth[0] > pageWidth[1] + 1) {
      throw new Error(`long capability sentences still push the page wide: ${pageWidth.join('/')}`)
    }

    if (errors.length) throw new Error(`console errors: ${JSON.stringify(errors.slice(0, 3))}`)
    console.log(`PASS runs-gate: form first, rows ${unique[0]}px, path 2+N ok, drawer ok, callout ok`)
  } catch (err) {
    console.log(`FAIL runs-gate: ${err.message}`)
    await page.screenshot({ path: 'tests/browser/runs-gate-fail.png', fullPage: true }).catch(() => {})
    return 1
  } finally {
    await browser.close()
    stopVite(vite)
  }
  return 0
}

process.exit(await main())
