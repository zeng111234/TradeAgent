/**
 * One-off: screenshot the sender-mailbox page against the real backend.
 *
 * Not a gate -- a look. The two accounts are created through the same API the
 * page uses and deleted again afterwards, so the only thing left behind is
 * the PNG. Run with the backend already up on :8000.
 */
import { writeFileSync } from 'node:fs'

import { APP_URL, PORT, chromiumPath, startVite, stopVite, waitForServer } from './harness.mjs'

const OUT = process.env.SHOT || 'senders-page.png'

async function api(path, options = {}) {
  const response = await fetch(`http://127.0.0.1:8000/api/v1${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  // 204 has no body; parsing it as JSON throws.
  const text = await response.text()
  return text ? JSON.parse(text) : null
}

const vite = startVite()
if (!(await waitForServer(APP_URL, 60_000))) {
  console.error('vite did not start:\n' + vite.log)
  stopVite(vite)
  process.exit(1)
}

const created = []
try {
  created.push(await api('/senders', {
    method: 'POST',
    body: JSON.stringify({
      label: 'QQ 主邮箱', host: 'smtp.qq.com', port: 465, use_tls: true,
      username: '2421258616@qq.com', password: 'probe', activate: true,
    }),
  }))
  created.push(await api('/senders', {
    method: 'POST',
    body: JSON.stringify({
      label: '163 备用邮箱', host: 'smtp.163.com', port: 25, use_tls: false,
      username: 'trade@163.com', password: 'probe',
    }),
  }))

  const { chromium } = await import('playwright-core')
  const browser = await chromium.launch({ executablePath: chromiumPath() })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  await page.goto(`${APP_URL}/senders`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(600)
  await page.screenshot({ path: OUT, fullPage: true })
  await browser.close()
  console.log('wrote', OUT)
} finally {
  for (const row of created) {
    if (row?.id) await api(`/senders/${row.id}`, { method: 'DELETE' })
  }
  stopVite(vite)
}
void PORT
