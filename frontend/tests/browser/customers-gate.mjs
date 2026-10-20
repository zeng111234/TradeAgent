/**
 * Gate for the Customers rearrangement: row click reaches the detail page,
 * Edit/Delete keep their dialogs, pagination pages, and the Industry cell
 * stays on one line (tooltip carries the overflow).
 */
import { chromiumPath, startVite, stopVite, APP_URL, waitForServer } from './harness.mjs'
import { chromium } from 'playwright-core'

const PAGE_SIZE = 20
const ROWS = Array.from({ length: 25 }, (_, i) => ({
  id: i + 1,
  company_name: `Gate Customer ${i + 1} Import-Export Trading GmbH`,
  country: ['Germany', 'Spain', 'Japan', 'United States'][i % 4],
  city: 'Hamburg',
  website: '',
  industry: i % 3 === 0 ? 'Textile Manufacturing and Industrial Yarn Wholesale Trade' : 'Textiles',
  products: '',
  stage: ['new', 'interested', 'quoting', 'ordering'][i % 4],
  source: ['manual', 'import', 'scraper'][i % 3],
  score: 88 - i * 5,
  tags: '',
  contacts: i % 2 ? [{}, {}] : [{}],
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
}))

const listPage = (pageNo, pageSize = PAGE_SIZE) => {
  const start = (pageNo - 1) * pageSize
  return { items: ROWS.slice(start, start + pageSize), total: ROWS.length, page: pageNo, page_size: pageSize }
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
    await page.route('**/api/v1/customers*', (route) => {
      const url = new URL(route.request().url())
      if (route.request().method() === 'GET' && !url.pathname.match(/\/customers\/\d+/)) {
        return route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(
            listPage(
              Number(url.searchParams.get('page') || 1),
              Number(url.searchParams.get('page_size') || PAGE_SIZE)
            )
          ),
        })
      }
      return route.continue()
    })

    await page.goto(`${APP_URL}/customers`, { waitUntil: 'domcontentloaded', timeout: 60_000 })
    await page.waitForTimeout(900)

    // 1. Industry must be one line per row: the old 140px dead width wrapped
    //    every long value and made row heights 39/56 in the same table.
    const heights = await page.locator('.el-table__body tr.el-table__row').evaluateAll((rows) =>
      rows.map((r) => Math.round(r.getBoundingClientRect().height)))
    const unique = [...new Set(heights)]
    if (unique.length !== 1) throw new Error(`row heights not uniform: ${JSON.stringify(unique)}`)

    // 2. Country folded under the company name; no Country column header.
    const headers = await page.locator('.el-table__header th').allInnerTexts()
    if (headers.some((h) => h.trim() === 'Country')) throw new Error('Country should be folded into Company')
    const secondLines = await page.locator('.el-table__body .cell-stack .hint').count()
    if (secondLines < 8) throw new Error(`country second line missing: ${secondLines}`)

    // 3. Row click reaches the detail page; Edit opens a dialog; Delete asks
    //    first. First row, not a name filter: "Gate Customer 1" also matches
    //    "Gate Customer 10" in a text filter.
    const firstRow = page.locator('.el-table__row').first()
    await firstRow.click()
    await page.waitForTimeout(600)
    if (!page.url().includes('/customers/')) {
      throw new Error(`row click did not reach detail: ${page.url()}`)
    }
    await page.goBack()
    await page.waitForTimeout(600)

    await firstRow.locator('button', { hasText: 'Edit' }).click()
    await page.waitForTimeout(300)
    const dialog = await page.locator('.el-dialog:visible').count()
    if (!dialog) throw new Error('Edit dialog did not open')
    await page.locator('.el-dialog:visible button', { hasText: 'Cancel' }).click()
    await page.waitForTimeout(200)

    page.once('dialog', (d) => d.dismiss().catch(() => {}))
    await firstRow.locator('button', { hasText: 'Delete' }).click()
    await page.waitForTimeout(400)
    const confirmBox = await page.locator('.el-message-box:visible').count()
    if (!confirmBox) throw new Error('Delete did not ask for confirmation')

    // 4. Pagination pages: 25 rows at 20 per page leaves 5 on page 2. Close the
    //    confirm first and wait for it to detach -- an open message box eats clicks.
    await page.keyboard.press('Escape').catch(() => {})
    await page.locator('.el-message-box:visible .el-message-box__btns .el-button:not(.el-button--primary)').first().click().catch(() => {})
    await page.waitForTimeout(400)
    if (await page.locator('.el-message-box:visible').count()) {
      await page.keyboard.press('Escape')
      await page.waitForTimeout(300)
    }
    const pager = page.locator('.el-pagination')
    if (!(await pager.count())) {
      throw new Error(`pagination missing; rows=${await page.locator('.el-table__row').count()} body=${(await page.locator('body').innerText()).slice(0, 200)}`)
    }
    await page.locator('.el-pager li', { hasText: '2' }).first().click({ timeout: 8000 }).catch(async (e) => {
      throw new Error(`pager click failed: ${e.message.split('\n')[0]} | pager=${await pager.innerHTML()}`)
    })
    await page.waitForTimeout(500)
    const page2 = await page.locator('.el-table__body tr.el-table__row').count()
    if (page2 !== 5) throw new Error(`page 2 should have 5 rows, got ${page2}`)
    if (errors.length) throw new Error(`console errors: ${JSON.stringify(errors.slice(0, 3))}`)
    console.log(`PASS customers-gate: uniform rows ${unique[0]}px, country folded, dialogs ok, paging ok`)
  } catch (err) {
    console.log(`FAIL customers-gate: ${err.message}`)
    await page.screenshot({ path: 'tests/browser/customers-gate-fail.png', fullPage: true }).catch(() => {})
    return 1
  } finally {
    await browser.close()
    stopVite(vite)
  }
  return 0
}

process.exit(await main())
