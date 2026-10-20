/**
 * One-off gate check for the Dashboard rearrangement: checkbox selection and
 * the draft popover must still work after the column merge. Run against the
 * dev server the suite starts on PORT 5199.
 */
import { chromiumPath, startVite, stopVite, APP_URL, PORT, waitForServer } from './harness.mjs'
import { chromium } from 'playwright-core'

const LEAD_FIXTURES = Array.from({ length: 6 }, (_, i) => ({
  company_name: `Gate Trading ${i + 1} GmbH`,
  country: 'Germany',
  relevance_score: 92 - i * 8,
  email: i % 3 === 2 ? '' : `buyer${i + 1}@gate.example`,
  already_exists: i === 4,
  website: `https://gate-${i + 1}.example`,
  draft_subject: `Introduction ${i + 1}`,
  draft_body: 'Dear buyer, embroidery thread, Ningbo. '.repeat(4),
}))

const PREVIEW_FIXTURES = {
  scan_date: '2026-10-20',
  keywords_used: ['embroidery thread'],
  leads: Array.from({ length: 4 }, (_, i) => ({
    company_name: `Preview Import ${i + 1} Handelsgesellschaft`,
    country: 'Spain',
    keyword: 'embroidery thread',
    relevance_score: 88 - i * 10,
    emails: i % 2 ? [] : [`import${i + 1}@gate.example`],
    website: `https://preview-${i + 1}.example`,
    draft_email: i === 2 ? '' : 'Dear buyer, our Q4 range. '.repeat(3),
  })),
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
    await page.route('**/api/v1/agent/scan-leads-draft', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ leads: LEAD_FIXTURES, total_found: 6, with_email: 4 }) }))
    await page.route('**/api/v1/agent/import-leads/preview', (r) =>
      r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PREVIEW_FIXTURES) }))

    await page.goto(`${APP_URL}/dashboard`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
    await page.waitForTimeout(800)

    // 1. page-head holds the three actions; only Full Scan is solid primary
    const actions = await page.locator('.page-header__actions button').allInnerTexts()
    if (actions.length !== 3) throw new Error(`expected 3 page actions, got ${JSON.stringify(actions)}`)
    const primary = await page.locator('.page-header__actions .el-button--primary').allInnerTexts()
    if (primary.length !== 1 || !primary[0].includes('Full Scan')) {
      throw new Error(`expected exactly one primary (Full Scan), got ${JSON.stringify(primary)}`)
    }

    // 2. KPI: four stat cards + a four-cell strip
    const cards = await page.locator('.stat-grid .stat-card').count()
    const strip = await page.locator('.kpi-strip .kpi-strip__item').count()
    if (cards !== 4 || strip !== 4) throw new Error(`KPI shape wrong: ${cards} cards / ${strip} strip cells`)

    // 3. scan -> table rows render, checkbox selects and drives the count
    await page.getByRole('button', { name: /搜索新客户/ }).click()
    await page.waitForTimeout(1000)
    const rows = await page.locator('.el-table__row').count()
    if (rows < 6) throw new Error(`expected scanned rows, got ${rows}`)

    const boxes = page.locator('.el-table__row .el-checkbox:not(.is-disabled)')
    const boxCount = await boxes.count()
    if (boxCount < 3) throw new Error(`expected selectable checkboxes, got ${boxCount}`)
    const label = () => page.locator('button', { hasText: 'Send Selected' }).first().innerText()
    // Scan auto-selects every eligible lead (has email, not already in CRM),
    // so the count starts at the eligible total and a click *removes* one.
    await page.waitForTimeout(150)
    const initial = await label()
    if (!initial.includes(`(${boxCount})`)) {
      throw new Error(`auto-select should start at ${boxCount} eligible: ${initial}`)
    }
    await boxes.nth(0).click()
    await page.waitForTimeout(150)
    const sendLabel = await label()
    if (!sendLabel.includes(`(${boxCount - 1})`)) {
      throw new Error(`clicking a selected box should deselect it: ${sendLabel}`)
    }

    // 4. draft popover opens on hover
    await page.locator('.cell-link', { hasText: '...' }).first().hover()
    await page.waitForTimeout(400)
    const popover = await page.locator('.el-popover:visible').count()
    if (!popover) throw new Error('draft popover did not open on hover')

    // 5. preview table: merged Draft cell keeps Ready + preview, Company keeps the site link
    await page.getByRole('button', { name: /Preview/ }).click()
    await page.waitForTimeout(800)
    const importRows = await page.locator('.el-table__row').count()
    const siteLinks = await page.locator('.cell-stack .cell-link').count()
    const readyTags = await page.locator('.cell-draft .el-tag').count()
    if (importRows < 4) throw new Error(`preview rows missing: ${importRows}`)
    if (siteLinks < 3) throw new Error(`website links folded into Company missing: ${siteLinks}`)
    if (readyTags < 2) throw new Error(`merged Draft cell lost its Ready tag: ${readyTags}`)

    if (errors.length) throw new Error(`console errors: ${JSON.stringify(errors.slice(0, 3))}`)
    console.log(`PASS dashboard-gate: ${rows} lead rows, ${boxCount} selectable, popover ok, import ${importRows} rows`)
  } catch (err) {
    console.log(`FAIL dashboard-gate: ${err.message}`)
    await page.screenshot({ path: 'tests/browser/dashboard-gate-fail.png', fullPage: true }).catch(() => {})
    return 1
  } finally {
    await browser.close()
    stopVite(vite)
  }
  return 0
}

process.exit(await main())
