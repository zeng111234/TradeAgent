import { chromium } from 'playwright-core'
import { chromiumPath, startVite, stopVite, APP_URL, waitForServer } from './harness.mjs'

/**
 * Shell + console gate.
 *
 * The agent-first layout is a product decision, so it gets a test like any
 * other: the console is the front door, the agent group sits above the data
 * group, the language switch actually re-renders, and a goal typed in the
 * topbar lands in the console and starts a run.
 */

const SNAPSHOT = {
  run_id: 'gate-console-run',
  goal: 'Gate goal',
  status: 'succeeded',
  rounds: 2,
  tool_calls: 3,
  elapsed_ms: 4100,
  stop_reason: 'answer',
  steps: [
    {
      round: 1,
      thought: 'Look at the pipeline first.',
      latency: 1.2,
      tool_calls: [{ name: 'list_customers', ok: true, elapsed: 0.2, arguments: { stage: 'quoting' }, observation: '[]' }],
    },
    {
      round: 2,
      thought: 'Enough to answer.',
      latency: 1.8,
      tool_calls: [
        { name: 'list_tasks', ok: true, elapsed: 0.1, arguments: {}, observation: '[]' },
        { name: 'get_customer', ok: true, elapsed: 0.2, arguments: { customer_id: 2 }, observation: '{}' },
      ],
    },
  ],
  final_output: '## Gate answer\n\nFollow up with EuroTextil today.',
}

async function main() {
  const vite = startVite()
  if (!(await waitForServer(APP_URL))) {
    console.log(vite.log)
    stopVite(vite)
    return 3
  }

  const browser = await chromium.launch({ executablePath: await chromiumPath(), headless: !process.env.HEADED })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`))

  await page.route('**/api/v1/agent/capabilities', (r) => r.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ configured: true, model: 'gate', tool_count: 11, tools: [], cannot: [], write_capable: [], sending: '' }),
  }))
  await page.route('**/api/v1/agent/run', (r) => r.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(SNAPSHOT),
  }))
  await page.route('**/api/v1/agent/runs?*', (r) => r.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ runs: [] }),
  }))
  // The console now opens on a thread list rather than a run list; without a
  // stub the request 404s and the gate reads that as a console error.
  await page.route('**/api/v1/agent/conversations*', (r) => r.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ count: 0, conversations: [] }),
  }))

  try {
    // 1. The console is the front door: "/" redirects there.
    await page.goto(`${APP_URL}/`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
    await page.waitForTimeout(900)
    if (!page.url().endsWith('/console')) throw new Error(`"/" should land on /console, got ${page.url()}`)

    // 2. Sidebar order: the agent group comes first, console is its first item.
    const groups = await page.locator('.sidebar .el-menu-item-group__title').allInnerTexts()
    if (groups.length !== 2 || !/AGENT/i.test(groups[0])) {
      throw new Error(`expected AGENT then data group, got ${JSON.stringify(groups)}`)
    }
    const items = await page.locator('.sidebar .el-menu-item').allInnerTexts()
    if (!/控制台|Console/.test(items[0])) {
      throw new Error(`console should be the first nav item, got ${JSON.stringify(items.slice(0, 3))}`)
    }
    if (!/数据看板|Dashboard/.test(items[4])) {
      throw new Error(`dashboard should open the data group, got ${JSON.stringify(items)}`)
    }

    // 3. Language switch re-renders the shell and survives a reload.
    await page.locator('.lang-switch button', { hasText: 'EN' }).click()
    await page.waitForTimeout(250)
    const navEn = await page.locator('.sidebar .el-menu-item').first().innerText()
    if (navEn.trim() !== 'Console') throw new Error(`nav did not switch to English: ${navEn}`)
    const lang = await page.evaluate(() => localStorage.getItem('tradeagent.locale'))
    if (lang !== 'en') throw new Error(`locale was not persisted: ${lang}`)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(700)
    const navAfterReload = await page.locator('.sidebar .el-menu-item').first().innerText()
    if (navAfterReload.trim() !== 'Console') throw new Error('locale did not survive reload')
    await page.locator('.lang-switch button', { hasText: '中文' }).click()
    await page.waitForTimeout(250)

    // 4. Topbar command bar: a goal takes you to the console and runs it.
    await page.goto(`${APP_URL}/customers`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(700)
    await page.locator('.topbar-command input').fill('Gate goal from topbar')
    await page.locator('.topbar-command input').press('Enter')
    await page.waitForTimeout(1200)
    if (!page.url().includes('/console')) throw new Error(`command bar did not route to the console: ${page.url()}`)
    const userBubble = await page.locator('.msg-user').first().innerText()
    if (!userBubble.includes('Gate goal from topbar')) {
      throw new Error(`console did not receive the goal: ${userBubble}`)
    }

    // 5. The run completes and the answer renders; the query is cleared so a
    //    reload does not fire the same goal again.
    await page.waitForTimeout(600)
    if (page.url().includes('goal=')) throw new Error(`goal query was not cleared: ${page.url()}`)
    const conclusion = await page.locator('.conclusion').first().innerText()
    if (!conclusion.includes('Gate answer')) throw new Error(`answer missing: ${conclusion}`)
    const head = await page.locator('.agent-head').first().innerText()
    if (!/2 轮|2 rounds/.test(head)) throw new Error(`rounds not shown: ${head}`)

    // 6. Tool calls are collapsed until asked for, then expand.
    if (await page.locator('.trace-block .timeline').count()) {
      throw new Error('trace should start collapsed')
    }
    await page.locator('.trace-toggle').first().click()
    await page.waitForTimeout(250)
    if (!(await page.locator('.trace-block .timeline').count())) {
      throw new Error('trace did not expand')
    }

    // 7. New conversation clears the thread back to the greeting.
    await page.locator('.agent-foot button', { hasText: /新对话|New conversation/ }).click()
    await page.waitForTimeout(300)
    if (!(await page.locator('.console-empty h1').count())) {
      throw new Error('new conversation did not reset the thread')
    }

    if (errors.length) throw new Error(`console errors: ${errors.join(' | ').slice(0, 300)}`)

    console.log('PASS shell-gate: console is home, nav grouped, lang persists, command bar runs, trace toggles')
    return 0
  } catch (err) {
    console.log(`FAIL shell-gate: ${err.message}`)
    return 2
  } finally {
    await browser.close()
    stopVite(vite)
  }
}

main().then((code) => process.exit(code))
