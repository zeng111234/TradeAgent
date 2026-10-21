/**
 * Measures the layout, so "it looks cramped" becomes a number.
 *
 * Four pages x three widths, recording the five things the UI review found
 * wrong: the document scrolling sideways, a card whose body scrolls inside
 * itself (a table wider than its container), table rows with inconsistent
 * heights, a stat grid that overflows, and the sidebar footer overlapping the
 * last menu item.
 *
 *   node tests/browser/layout.mjs baseline
 *   node tests/browser/layout.mjs after-phase1
 *
 * Writes `artifacts/ui/layout-<label>.json` and one screenshot per page/width
 * in `artifacts/ui/<label>/`. Run it again with a different label after a
 * change and diff the two JSON files -- the numbers, not the screenshots, are
 * the gate.
 *
 * Needs a real backend: unlike flows.mjs this does not intercept the API,
 * because the defects being measured come from real data (long tool paths,
 * long industry names, a fourth stat card).
 *
 * Env: APP_URL (default http://127.0.0.1:5173), CHROMIUM_PATH, HEADED=1.
 */
import { mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright-core'

import { APP_URL, PORT, chromiumPath, startVite, stopVite, waitForServer } from './harness.mjs'

const LABEL = process.argv[2] || 'run'
const HERE = dirname(fileURLToPath(import.meta.url))
// Three levels up: frontend/tests/browser -> frontend -> repo root. Screenshots
// live with the other generated artefacts, not inside the frontend tree.
const OUT = join(HERE, '..', '..', '..', 'artifacts', 'ui')

const WIDTHS = [1280, 1440, 1920]
const HEIGHT = 900
const PAGES = [
  { path: '/console', name: 'console' },
  { path: '/dashboard', name: 'dashboard' },
  { path: '/customers', name: 'customers' },
  { path: '/runs', name: 'runs' },
  { path: '/quality', name: 'quality' },
]

//: Every view, not just the four being measured. A token change reaches all of
//: them -- Element Plus components are on every page -- and a page that fails
//: to render is not a layout defect, it is a blank screen.
const ALL_VIEWS = [
  '/console',
  '/dashboard',
  '/customers',
  '/customers/{id}',
  '/emails',
  '/templates',
  '/tasks',
  '/agent',
  '/runs',
  '/quality',
]

//: What `tokens.css` declares. Asserted against the live page rather than the
//: stylesheet, because the failure being guarded is an import *order*: the file
//: present, its effect absent, every test green.
const EXPECTED_PRIMARY = '#3e63dd'

/**
 * The two lead tables on the dashboard only render after a scan, which calls a
 * live model. With no rows there is nothing to measure, so the 1190px column
 * total -- the reason the table scrolls sideways at 1440 -- stays invisible.
 * Answering the scan endpoint with fixtures and clicking the button is the only
 * way to get real rows into the DOM without spending tokens.
 */
const LEAD_FIXTURES = Array.from({ length: 8 }, (_, i) => ({
  company_name: `Fixture Textile Trading ${i + 1} Handelsgesellschaft mbH`,
  country: ['Germany', 'Spain', 'Japan', 'United States'][i % 4],
  relevance_score: 92 - i * 6,
  email: i % 3 === 2 ? '' : `purchasing.manager${i + 1}@fixture-textile.example`,
  already_exists: i % 4 === 3,
  website: `https://fixture-textile-${i + 1}.example`,
  draft_subject: `Introduction - embroidery thread for Fixture Textile ${i + 1}`,
  draft_body:
    'Dear purchasing manager, we manufacture embroidery thread and metallic yarn ' +
    'in Ningbo and would like to introduce our Q4 range to your team. '.repeat(3),
}))

async function loadDashboardFixtures(page) {
  await page.route('**/api/v1/agent/scan-leads-draft', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ leads: LEAD_FIXTURES, total_found: 8, with_email: 5 }),
    })
  )
  await page.getByRole('button', { name: /搜索新客户/ }).click()
  await page.waitForTimeout(1200)
}

async function readTheme(page) {
  return page.evaluate(() => {
    const root = getComputedStyle(document.documentElement)
    const button = document.querySelector('.el-button--primary')
    return {
      declared: root.getPropertyValue('--el-color-primary').trim(),
      buttonBackground: button ? getComputedStyle(button).backgroundColor : '',
    }
  })
}

async function visitAllViews(browser, base, customerId) {
  const page = await browser.newPage({ viewport: { width: 1440, height: HEIGHT } })
  const errors = []
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text())
  })
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`))

  const report = []
  try {
    for (const template of ALL_VIEWS) {
      const path = template.replace('{id}', String(customerId))
      errors.length = 0
      const response = await page.goto(`${base}${path}`, {
        waitUntil: 'domcontentloaded',
        timeout: 60_000,
      })
      await page.waitForTimeout(900)
      const chars = await page.evaluate(() => document.body.innerText.trim().length)
      report.push({
        path,
        status: response ? response.status() : 0,
        chars,
        errors: errors.slice(0, 2),
      })
    }
  } finally {
    await page.close()
  }
  return report
}

/**
 * Shell checks that the four-page sweep cannot express.
 *
 * The footer overlap only appears on a short window (the sidebar scrolled, the
 * footer was absolutely positioned), and the content cap only shows on a wide
 * one. Both are one-off measurements, not per-page ones.
 */
async function checkShell(browser, base) {
  // 520px tall on purpose: at 700 the eight menu items still fit, so the
  // "does the menu scroll instead of the footer" question has no answer.
  const short = await browser.newPage({ viewport: { width: 1440, height: 520 } })
  let footer = null
  try {
    await short.goto(`${base}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
    await short.waitForTimeout(900)
    footer = await short.evaluate(() => {
      const el = document.querySelector('.sidebar-footer')
      const items = [...document.querySelectorAll('.sidebar .el-menu-item')]
      const last = items[items.length - 1]
      const menu = document.querySelector('.sidebar .el-menu')
      const scrolls = menu ? menu.scrollHeight > menu.clientHeight + 1 : false
      // When the menu scrolls, the last item is legitimately below the fold;
      // the question is whether the footer overlaps the menu *box*.
      const boundary = scrolls ? menu : last
      return {
        gap: el && boundary
          ? Math.round(el.getBoundingClientRect().top - boundary.getBoundingClientRect().bottom)
          : null,
        menuScrolls: scrolls,
        footerVisible: el ? el.getBoundingClientRect().bottom <= window.innerHeight + 1 : null,
      }
    })
  } finally {
    await short.close()
  }

  const wide = await browser.newPage({ viewport: { width: 1920, height: 900 } })
  let content = null
  try {
    await wide.goto(`${base}/customers`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
    await wide.waitForTimeout(900)
    content = await wide.evaluate(() => {
      const main = document.querySelector('.main-content')
      const first = main ? main.firstElementChild : null
      const sidebar = document.querySelector('.sidebar')
      const sidebarWidth = sidebar ? sidebar.getBoundingClientRect().width : 0
      const rect = first ? first.getBoundingClientRect() : null
      return {
        mainWidth: main ? Math.round(main.getBoundingClientRect().width) : null,
        contentWidth: rect ? Math.round(rect.width) : null,
        // Gutters measured against the space right of the sidebar, not the
        // viewport: the sidebar is part of the layout, not page margin.
        leftGutter: rect ? Math.round(rect.left - sidebarWidth) : null,
        rightGutter: rect ? Math.round(document.body.clientWidth - rect.right) : null,
      }
    })
  } finally {
    await wide.close()
  }

  return { footer, content }
}

async function measure(page) {  return page.evaluate(() => {
    const root = document.scrollingElement
    const main = document.querySelector('.main-content')

    const cardOverflow = [...document.querySelectorAll('.el-card__body')]
      .map((el) => Math.round(el.scrollWidth - el.clientWidth))
      .filter((delta) => delta > 1)

    // A wide table does not widen `.el-card__body`: Element Plus puts the
    // horizontal scroll on an inner scrollbar wrap, so the card-level check
    // above is blind to exactly the defect being hunted. Compare the rendered
    // table width against the viewport that clips it.
    const tableOverflow = [...document.querySelectorAll('.el-table')]
      .map((el) => {
        const wrap = el.querySelector('.el-scrollbar__wrap') || el.querySelector('.el-table__body-wrapper')
        const table = el.querySelector('.el-table__body')
        if (!wrap || !table) return 0
        return Math.round(table.getBoundingClientRect().width - wrap.clientWidth)
      })
      .filter((delta) => delta > 1)

    const rows = [...document.querySelectorAll('.el-table__body tr.el-table__row')]
      .map((row) => Math.round(row.getBoundingClientRect().height))
      .filter((h) => h > 0)

    const grid = document.querySelector('.stat-grid')
    const gridOverflow = grid ? Math.round(grid.scrollWidth - grid.clientWidth) : null
    const gridTops = grid
      ? [...grid.children].map((el) => Math.round(el.getBoundingClientRect().top))
      : []

    const footer = document.querySelector('.sidebar-footer')
    const items = [...document.querySelectorAll('.sidebar .el-menu-item')]
    const lastItem = items[items.length - 1]
    const overlap =
      footer && lastItem
        ? Math.round(footer.getBoundingClientRect().top - lastItem.getBoundingClientRect().bottom)
        : null

    return {
      innerWidth: window.innerWidth,
      docScrollWidth: root.scrollWidth,
      docOverflow: Math.round(root.scrollWidth - root.clientWidth),
      mainOverflow: main ? Math.round(main.scrollWidth - main.clientWidth) : null,
      cardOverflowWorst: cardOverflow.length ? Math.max(...cardOverflow) : 0,
      cardOverflowCount: cardOverflow.length,
      tableOverflowWorst: tableOverflow.length ? Math.max(...tableOverflow) : 0,
      tableOverflowCount: tableOverflow.length,
      rowHeights: [...new Set(rows)].sort((a, b) => a - b),
      rowCount: rows.length,
      gridOverflow,
      gridCardTops: [...new Set(gridTops)],
      sidebarFooterGap: overlap,
    }
  })
}

function summarise(record) {
  const issues = []
  if (record.docOverflow > 0) issues.push(`doc scrolls ${record.docOverflow}px`)
  if (record.cardOverflowCount > 0) {
    issues.push(`${record.cardOverflowCount} card body scrolls (worst ${record.cardOverflowWorst}px)`)
  }
  if (record.tableOverflowCount > 0) {
    issues.push(`${record.tableOverflowCount} table scrolls sideways (worst ${record.tableOverflowWorst}px)`)
  }
  if (record.rowHeights.length > 1) issues.push(`row heights ${record.rowHeights.join('/')}`)
  if (record.gridOverflow > 0) issues.push(`stat grid scrolls ${record.gridOverflow}px`)
  if (record.gridCardTops.length > 1) issues.push(`kpi cards on ${record.gridCardTops.length} baselines`)
  if (record.sidebarFooterGap !== null && record.sidebarFooterGap < 0) {
    issues.push(`sidebar footer overlaps menu by ${-record.sidebarFooterGap}px`)
  }
  return issues
}

async function main() {
  const exe = chromiumPath()
  if (!exe) {
    console.error('no Chromium found; set CHROMIUM_PATH')
    return 3
  }

  let vite = null
  if (!(await waitForServer(`${APP_URL}/`, 1500))) {
    console.log(`starting vite on ${PORT} ...`)
    vite = startVite()
    if (!(await waitForServer(`${APP_URL}/`, 90_000))) {
      console.error('vite did not start')
      stopVite(vite)
      return 3
    }
  }

  const api = await fetch(`${APP_URL}/api/v1/analytics/dashboard`).catch(() => null)
  if (!api || !api.ok) {
    console.error(
      'the API is not reachable through this origin; start the backend first ' +
        '(uvicorn on :8000 with the demo database)'
    )
    stopVite(vite)
    return 3
  }

  mkdirSync(join(OUT, LABEL), { recursive: true })
  const browser = await chromium.launch({ executablePath: exe, headless: !process.env.HEADED })
  const results = []
  let theme = null
  let views = []
  let shell = null
  let customerId = 1

  try {
    const probe = await browser.newPage({ viewport: { width: 1440, height: HEIGHT } })
    await probe.goto(`${APP_URL}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
    theme = await readTheme(probe)
    await probe.close()

    const list = await fetch(`${APP_URL}/api/v1/customers?page=1&page_size=1`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
    if (list?.items?.length) customerId = list.items[0].id

    for (const width of WIDTHS) {
      const page = await browser.newPage({ viewport: { width, height: HEIGHT } })
      for (const { path, name } of PAGES) {
        // ``domcontentloaded`` rather than ``networkidle``: the run page keeps an
        // SSE connection open, so networkidle would wait forever.
        await page.goto(`${APP_URL}${path}`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
        await page.waitForTimeout(1500)
        if (name === 'dashboard') await loadDashboardFixtures(page)
        // Clicking the fixture button scrolls the button into view, and the
        // screenshot would then start below the KPI cards. Measure what the
        // page looks like from the top.
        await page.evaluate(() => window.scrollTo(0, 0))
        await page.waitForTimeout(200)
        const record = await measure(page)
        results.push({ page: name, width, ...record })
        await page.screenshot({
          path: join(OUT, LABEL, `${name}-${width}.png`),
          fullPage: false,
        })
      }
      await page.close()
    }

    views = await visitAllViews(browser, APP_URL, customerId)
    shell = await checkShell(browser, APP_URL)
  } finally {
    await browser.close()
    stopVite(vite)
  }

  writeFileSync(
    join(OUT, `layout-${LABEL}.json`),
    JSON.stringify({ theme, views, measurements: results }, null, 2),
    'utf8'
  )

  console.log(`\n${'page'.padEnd(12)}${'width'.padEnd(7)}${'doc'.padEnd(6)}${'cards'.padEnd(8)}${'rows'.padEnd(12)}notes`)
  let problems = 0
  for (const r of results) {
    const issues = summarise(r)
    if (issues.length) problems++
    console.log(
      `${r.page.padEnd(12)}${String(r.width).padEnd(7)}${String(r.docOverflow).padEnd(6)}` +
        `${String(r.cardOverflowCount).padEnd(8)}` +
        `${(r.rowHeights.join('/') || '-').padEnd(12)}${issues.join('; ') || 'ok'}`
    )
  }
  console.log(`\n${problems} of ${results.length} page/width combinations show a layout defect`)

  const themeOk = theme?.declared === EXPECTED_PRIMARY
  console.log(
    `theme: --el-color-primary = ${theme?.declared || '(missing)'}` +
      ` (expected ${EXPECTED_PRIMARY}) ${themeOk ? 'ok' : 'MISMATCH'}` +
      (theme?.buttonBackground ? `, primary button ${theme.buttonBackground}` : '')
  )

  const brokenViews = views.filter((v) => v.status !== 200 || v.chars < 20 || v.errors.length)
  for (const v of brokenViews) {
    console.log(`  BROKEN ${v.path} status=${v.status} chars=${v.chars} ${v.errors.join(' | ')}`)
  }
  console.log(`views: ${views.length - brokenViews.length}/${views.length} render without errors`)

  const footerGap = shell?.footer?.gap
  const content = shell?.content
  console.log(
    `shell: footer gap ${footerGap === null ? 'n/a' : `${footerGap}px`}` +
      ` (1440x520), menu scrolls ${shell?.footer?.menuScrolls},` +
      ` content ${content?.contentWidth}px with gutters ${content?.leftGutter}/${content?.rightGutter}` 
  )

  const shellOk =
    (footerGap === null || footerGap >= 0) &&
    (content?.contentWidth === null || content.contentWidth <= 1440) &&
    // The menu must absorb the shortfall, and the footer must stay on screen.
    (shell?.footer?.menuScrolls !== false || footerGap === null) &&
    (shell?.footer?.footerVisible !== false)

  console.log(`written: artifacts/ui/layout-${LABEL}.json, screenshots in artifacts/ui/${LABEL}/`)

  if (!themeOk || brokenViews.length || !shellOk) return 2
  return 0
}

main().then((code) => process.exit(code)).catch((err) => {
  console.error('FAILED', err)
  process.exit(1)
})
