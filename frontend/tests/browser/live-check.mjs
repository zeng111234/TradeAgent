/**
 * One-off: prove the published site actually works, end to end.
 *
 * Not a gate -- a demonstration against the real public host. It drives a real
 * browser: cold load (must bounce to the gate), sign in, then every route,
 * watching for console errors and for pages that render empty. It also writes
 * through the same API the pages use and reads the rows back, because a page
 * that renders proves nothing about whether anything is stored.
 *
 * Everything it creates is deleted again in the `finally` block.
 */
import { chromiumPath } from './harness.mjs'

const BASE = process.env.LIVE_BASE || 'https://tradeagent.app.workbuddy.host'
const CODE = process.env.LIVE_CODE || ''
// No default on purpose: this file is committed to a public repo, and a
// default here would publish the gate's password.
const PASSWORD = process.env.LIVE_PASSWORD || ''

// The share code is what lets the platform route us to the app; it is also
// remembered in a cookie, but carrying it on every hop keeps the check honest.
const url = (path) => `${BASE}${path}${CODE ? `?sharecode=${CODE}` : ''}`

const ROUTES = [
  '/console',
  '/dashboard',
  '/customers',
  '/customers/1',
  '/emails',
  '/templates',
  '/tasks',
  '/agent',
  '/runs',
  '/quality',
  '/senders',
]

const problems = []
const note = (message) => {
  problems.push(message)
  console.log('  !! ' + message)
}

const { chromium } = await import('playwright-core')
const browser = await chromium.launch({ executablePath: chromiumPath() })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

const consoleErrors = []
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text())
})
page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message))

async function inPage(fn, ...args) {
  // Runs with the session cookie already attached, same-origin.
  return page.evaluate(fn, ...args)
}

async function api(method, path, body) {
  return inPage(
    async ({ method, path, body }) => {
      const response = await fetch(`/api/v1${path}`, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      const text = await response.text()
      return { status: response.status, body: text ? JSON.parse(text) : null }
    },
    { method, path, body },
  )
}

const created = { tasks: [], senders: [], notes: [] }

try {
  // ---------------------------------------------------------------- gate
  console.log('1. cold load without a session')
  await page.goto(url('/'), { waitUntil: 'networkidle' })
  console.log('   landed on:', page.url().replace(BASE, '') || '/')
  if (!page.url().includes('/login')) note('an unauthenticated visit did not reach the gate')

  console.log('2. sign in')
  await page.fill('input[type=password]', PASSWORD)
  await page.click('button[type=submit]')
  await page.waitForLoadState('networkidle')
  console.log('   after sign-in:', page.url().replace(BASE, ''))
  if (page.url().includes('/login')) note('the password was rejected')

  // ------------------------------------------------------------- routes
  console.log('3. every route, watching for blank pages')
  for (const route of ROUTES) {
    await page.goto(url(route), { waitUntil: 'networkidle' })
    await page.waitForTimeout(900)
    const text = (await page.locator('body').innerText()).trim()
    const rows = await page.locator('.el-table__row').count()
    const spinner = await page.locator('.el-loading-mask').count()
    const landed = page.url().replace(BASE, '')
    console.log(
      `   ${route.padEnd(15)} text=${String(text.length).padStart(5)}  rows=${rows}  loading=${spinner}  url=${landed}`,
    )
    if (text.length < 80) note(`${route} rendered almost nothing (${text.length} chars)`)
    if (/无法连接|请求失败|Network Error|加载失败/.test(text)) note(`${route} shows a failure message`)
    if (spinner > 0) note(`${route} is stuck on a loading mask`)
    if (landed.includes('/login')) note(`${route} bounced back to the gate`)
  }

  // --------------------------------------------------------- real writes
  console.log('4. write through the API the pages use, then read back')

  const customers = await api('GET', '/customers?limit=3')
  const total = customers.body?.total ?? customers.body?.length ?? 0
  console.log(`   customers: ${total}`)
  if (!total) note('the customer list is empty -- nothing to attach a note to')

  const firstId = customers.body?.items?.[0]?.id ?? customers.body?.[0]?.id

  const task = await api('POST', '/tasks', {
    title: 'LIVE-CHECK 线上写入探针',
    priority: 'low',
    task_type: 'follow_up',
    customer_id: firstId ?? null,
  })
  console.log('   create task ->', task.status)
  if (task.status >= 300) note(`creating a task failed (${task.status})`)
  if (task.body?.id) created.tasks.push(task.body.id)

  const tasks = await api('GET', '/tasks')
  const found = (tasks.body ?? []).some((r) => r.title === 'LIVE-CHECK 线上写入探针')
  console.log('   task visible in list ->', found)
  if (!found) note('the task was created but does not come back in the list')

  const mailbox = await api('POST', '/senders', {
    label: 'LIVE-CHECK 探针邮箱',
    host: 'smtp.invalid.test',
    port: 465,
    use_tls: true,
    username: 'probe@invalid.test',
    password: 'probe',
  })
  console.log('   create mailbox ->', mailbox.status)
  if (mailbox.status >= 300) note(`creating a mailbox failed (${mailbox.status})`)
  if (mailbox.body?.id) created.senders.push(mailbox.body.id)

  const effective = await api('GET', '/senders/effective')
  console.log('   effective sender ->', JSON.stringify(effective.body))
  if (effective.status >= 300) note('the effective-sender endpoint did not answer')

  // ------------------------------------------------------------- the LLM
  console.log('5. one small agent run (this is the part that costs money)')
  const caps = await api('GET', '/agent/capabilities')
  console.log('   configured:', caps.body?.configured, '| tools:', caps.body?.tool_count)
  if (!caps.body?.configured) note('the agent reports "not configured" online')

  if (caps.body?.configured) {
    const run = await api('POST', '/agent/run', {
      goal: '用一句话回答：当前共有几个客户？只回答数字和一句中文说明。',
      wait: false,
    })
    const runId = run.body?.run_id
    console.log('   run started ->', run.status, runId)
    if (!runId) {
      note('no run id came back from the agent')
    } else {
      let state = null
      for (let i = 0; i < 30; i += 1) {
        await page.waitForTimeout(2000)
        state = await api('GET', `/agent/runs/${runId}`)
        // The API says "succeeded"; "completed" is what the older writers of
        // this repo would have guessed, and a check that guesses wrong is a
        // check that reports a working site as broken.
        if (state.body && ['succeeded', 'completed', 'failed', 'cancelled'].includes(state.body.status))
          break
      }
      const status = state?.body?.status
      const rounds = state?.body?.rounds
      const answer = (state?.body?.final_output ?? '').slice(0, 160)
      console.log('   run status:', status, '| rounds:', rounds)
      console.log('   answer:', answer)
      if (status !== 'completed') note(`the agent run ended as "${status}"`)
      if (!answer.trim()) note('the agent finished with no answer text')
    }
  }

  // ------------------------------------------------------------ pictures
  console.log('6. screenshots')
  for (const [route, file] of [
    ['/console', 'live-console.png'],
    ['/customers', 'live-customers.png'],
    ['/agent', 'live-agent.png'],
  ]) {
    await page.goto(url(route), { waitUntil: 'networkidle' })
    await page.waitForTimeout(1200)
    await page.screenshot({ path: file, fullPage: true })
    console.log('   wrote', file)
  }
} finally {
  for (const id of created.tasks) await api('DELETE', `/tasks/${id}`)
  for (const id of created.senders) await api('DELETE', `/senders/${id}`)
  await browser.close()
}

console.log('\nconsole errors on the page:', consoleErrors.length)
for (const line of consoleErrors.slice(0, 8)) console.log('   -', line.slice(0, 160))

console.log('\nverdict:', problems.length ? `${problems.length} problem(s)` : 'the published site works')
for (const line of problems) console.log('   !! ' + line)
