/**
 * Agent-page gate.
 *
 * The page is ten tools behind one tab strip, and every one of them renders
 * the same two shapes: a card per customer (with a severity edge) and a
 * generated-mail panel. This checks the shapes survive a refactor -- edge
 * class per risk band, stage labels through the status map, the mail panel
 * present when a draft is open -- and that the page itself renders at all.
 *
 *   node tests/browser/agent-gate.mjs
 *
 * Intercepts the API; no backend needed. Env: APP_URL, CHROMIUM_PATH, HEADED=1.
 */
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright-core'

import { APP_URL, chromiumPath, startVite, stopVite, waitForServer } from './harness.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))

const CHURN = {
  total_alerts: 3, critical: 1, high: 1, medium: 1,
  alerts: [
    {
      customer_id: 1, risk_level: 'critical', company_name: 'Acme GmbH',
      stage: 'quoting', country: 'Germany',
      risk_reasons: ['No reply in 21 days'], suggested_actions: ['Send rescue email'],
      emails_sent: 4, emails_replied: 0, days_in_stage: 21,
      contact_email: 'a@acme.de', contact_name: 'Hans',
      rescue_email: '<p>Hi Hans, just checking in...</p>',
      rescue_subject: 'Checking in on your quotation',
    },
    {
      customer_id: 2, risk_level: 'high', company_name: 'Beta SA',
      stage: 'sample', country: 'France',
      risk_reasons: ['Sample sent 14 days ago'], suggested_actions: ['Call'],
      emails_sent: 2, emails_replied: 1, days_in_stage: 14,
      contact_email: 'b@beta.fr', contact_name: 'Marie',
    },
    {
      customer_id: 3, risk_level: 'medium', company_name: 'Gamma Ltd',
      stage: 'contacted', country: 'UK',
      risk_reasons: ['Only one touch so far'], suggested_actions: ['Nudge'],
      emails_sent: 1, emails_replied: 0, days_in_stage: 9,
      contact_email: 'c@gamma.co.uk', contact_name: 'Ian',
    },
  ],
}

const LEADS = {
  total_found: 2,
  ai_powered: true,
  market_insights: 'German buyers respond well to technical data sheets.',
  leads: [
    {
      company_name: 'Delta Handel', country: 'Germany', website: 'https://delta.de',
      relevance_score: 85, ai_priority: 'high', emails: ['buy@delta.de'], phones: ['+49 30 1'],
      ai_reason: 'Imports decorative yarn monthly', snippet: 'A long snippet that must ellipsize rather than grow the row',
    },
    {
      company_name: 'Epsilon Trade', country: 'France', website: 'https://epsilon.fr',
      relevance_score: 45, emails: [], phones: [],
    },
  ],
}

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
  // console errors from intercepted 4xx/5xx are part of the exercise, not the gate
  page.on('console', (m) => {
    const t = m.text()
    if (m.type() === 'error' && !/Failed to load resource|net::ERR|404|500/.test(t)) errors.push(t)
  })

  await page.route('**/api/v1/agent/**', async (route) => {
    const url = route.request().url()
    const j = (body) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (url.includes('holidays')) return j([{ name: 'Christmas', days_until: 12, upcoming: true }])
    if (url.includes('churn-alerts')) return j(CHURN)
    if (url.includes('scan-leads')) return j(LEADS)
    return j({})
  })

  try {
    await page.goto(`${APP_URL}/agent`, { waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.waitForTimeout(800)

    // 1. All ten tools are reachable from the tab strip.
    const tabs = await page.locator('.el-tabs__item').count()
    if (tabs !== 10) throw new Error(`expected 10 tool tabs, got ${tabs}`)

    // 2. Churn feed: one edge class per risk band, stage labels through the map.
    //    Element Plus keeps every tab pane mounted, so scope to the visible one.
    await page.locator('.el-tabs__item', { hasText: 'Churn Alerts' }).click()
    await page.locator('button', { hasText: 'Scan for At-Risk' }).click()
    await page.waitForTimeout(500)
    const churnPane = page.locator('.el-tab-pane:visible')
    const cards = churnPane.locator('.action-card--edge')
    const n = await cards.count()
    if (n !== 3) throw new Error(`expected 3 churn cards, got ${n}`)
    const edges = []
    for (let i = 0; i < n; i++) {
      edges.push((await cards.nth(i).getAttribute('class')).match(/edge-\w+/)?.[0] || '(none)')
    }
    const want = ['edge-negative', 'edge-warning', 'edge-neutral']
    for (let i = 0; i < 3; i++) {
      if (!edges[i].includes(want[i])) {
        throw new Error(`churn card ${i}: expected ${want[i]}, got ${edges[i]}`)
      }
    }
    // stage goes through stageLabel, so "quoting" reads "Quoting"
    const stageText = await churnPane.locator('.el-tag', { hasText: 'Quoting' }).count()
    if (!stageText) throw new Error('stage label was not normalised through stageLabel()')

    // 3. The generated-mail panel appears under a card when a draft is opened.
    await page.locator('button', { hasText: 'Rescue Email' }).first().click()
    await page.waitForTimeout(300)
    const panel = await churnPane.locator('.mail-panel--accent').count()
    if (!panel) throw new Error('rescue draft did not render the accent mail panel')
    const subject = await churnPane.locator('.subject-line').first().innerText()
    if (!subject.includes('Checking in')) throw new Error(`subject line wrong: ${subject}`)

    // 4. Lead feed: score band edges follow scoreBand (70/40), not the old 70/50.
    //    scanLeads refuses to run without keywords, so fill the form first.
    await page.locator('.el-tabs__item', { hasText: 'Lead Scanner' }).click()
    await page.locator('.el-tab-pane:visible input').first().fill('metallic yarn')
    await page.locator('button', { hasText: 'Scan for Leads' }).click()
    await page.waitForTimeout(500)
    const leadPane = page.locator('.el-tab-pane:visible')
    const leadCards = leadPane.locator('.action-card--edge')
    const ln = await leadCards.count()
    if (ln !== 2) throw new Error(`expected 2 lead cards, got ${ln}`)
    const l0 = await leadCards.nth(0).getAttribute('class')
    const l1 = await leadCards.nth(1).getAttribute('class')
    if (!l0.includes('edge-positive')) throw new Error(`score 85 should be edge-positive: ${l0}`)
    // 45 is mid under 70/40 -- it was low under the old 70/50 cut
    if (!l1.includes('edge-warning')) throw new Error(`score 45 should be edge-warning: ${l1}`)
    // The snippet must never widen its card. scrollWidth > clientWidth on the
    // snippet itself is the ellipsis working, not a defect -- what matters is
    // the width does not leak up to the card (which is what made 1280 scroll).
    const clip = await page.evaluate(() => {
      const pane = [...document.querySelectorAll('.el-tab-pane')].find((el) => el.offsetParent !== null)
      const s = pane?.querySelector('.snippet')
      if (!s) return { ok: false, why: 'no .snippet' }
      const card = s.closest('.action-card')
      return {
        ok: card.scrollWidth <= card.clientWidth + 1,
        card: [card.scrollWidth, card.clientWidth],
        snippet: [s.scrollWidth, s.clientWidth],
      }
    })
    if (!clip.ok) throw new Error(`snippet widened its card: ${JSON.stringify(clip)}`)

    // 5. Ten tools, no page errors.
    if (errors.length) throw new Error(`page errors: ${errors.slice(0, 3).join(' | ')}`)

    console.log('PASS agent-gate: 10 tabs, churn edges +/+/-, mail panel ok, lead edges70/40, snippet clipped')
    return 0
  } catch (e) {
    console.log(`FAIL agent-gate: ${e.message}`)
    try {
      await page.screenshot({ path: join(HERE, '..', '..', '..', 'artifacts', 'ui', 'agent-gate-fail.png') })
    } catch { /* best effort */ }
    return 1
  } finally {
    await browser.close()
    stopVite(vite)
  }
}

process.exit(await main())
