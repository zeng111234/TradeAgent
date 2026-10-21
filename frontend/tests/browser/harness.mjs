/**
 * Shared plumbing for the browser-level scripts.
 *
 * `flows.mjs` (behaviour) and `layout.mjs` (measurements) both need the same
 * three things: a Chromium binary that exists on this machine, a way to tell
 * whether vite is already up, and a vite that can be started and stopped
 * without leaking the port. Duplicating them meant two places to fix whenever
 * the browser cache moved -- and the second copy is the one that silently
 * rots.
 *
 * Chromium comes from the Playwright cache rather than a project dependency:
 * `playwright-core` is installed with `--no-save` (see README) because adding
 * it to package.json without regenerating the lockfile would break `npm ci`.
 */
import { spawn } from 'node:child_process'
import { existsSync, readdirSync, statSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

export const APP_URL = process.env.APP_URL || 'http://127.0.0.1:5199'
export const PORT = new URL(APP_URL).port || '5199'

export function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH

  const roots = [
    process.env.PLAYWRIGHT_BROWSERS_PATH,
    join(homedir(), 'AppData', 'Local', 'ms-playwright'),
    join(homedir(), '.cache', 'ms-playwright'),
    join(homedir(), 'Library', 'Caches', 'ms-playwright'),
  ].filter(Boolean)

  // The unpacked folder is named after the archive Playwright fetched, and
  // that name is not stable: older builds unpack to `chrome-linux`, the
  // Chrome-for-Testing builds to `chrome-linux64`. Hard-coding one of them
  // works on whichever machine it was written on and fails on the other --
  // which is exactly how a green local run turns into a red CI run that says
  // "No Chromium found" while the browser sits there downloaded. So read the
  // build directory instead of guessing it.
  const isDir = (path) => {
    try {
      return statSync(path).isDirectory()
    } catch {
      return false
    }
  }

  const candidates = []
  for (const root of roots) {
    if (!existsSync(root)) continue
    for (const entry of readdirSync(root)) {
      // chromium-<rev> for the full browser, chromium_headless_shell-<rev>
      // for the shell. Both unpack their binary one level down.
      if (!entry.startsWith('chromium')) continue
      const build = join(root, entry)
      if (!isDir(build)) continue
      for (const inner of readdirSync(build)) {
        if (!inner.startsWith('chrome-')) continue
        const folder = join(build, inner)
        if (!isDir(folder)) continue
        candidates.push(join(folder, 'chrome.exe'))
        candidates.push(join(folder, 'chrome'))
        // macOS nests the binary inside an app bundle, one level deeper.
        candidates.push(join(folder, 'Chromium.app', 'Contents', 'MacOS', 'Chromium'))
      }
    }
  }
  return candidates.find((path) => existsSync(path)) || ''
}

export async function waitForServer(url, timeoutMs = 60_000) {
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

export function startVite() {
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

export function stopVite(child) {
  if (!child) return
  if (process.platform === 'win32') {
    // The shell wrapper survives a plain kill() and keeps the port bound.
    spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' })
  } else {
    child.kill()
  }
}
