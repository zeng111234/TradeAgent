/**
 * zh and en must stay the same shape.
 *
 * The fallback in `t()` returns the key when a translation is missing, so a
 * missing English string does not crash -- it silently renders `console.copy`
 * to an English reader. That is the kind of bug nobody reports because it looks
 * like a styling problem. This test makes the two dictionaries agree.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import zh from '../src/i18n/zh.js'
import en from '../src/i18n/en.js'

/** Flatten a nested dictionary into dotted paths. */
function flatten(value, prefix = '', out = []) {
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (child && typeof child === 'object' && !Array.isArray(child)) {
      flatten(child, path, out)
    } else {
      out.push(path)
    }
  }
  return out
}

test('every key exists in both languages', () => {
  const zhKeys = new Set(flatten(zh))
  const enKeys = new Set(flatten(en))

  const missingInEn = [...zhKeys].filter((key) => !enKeys.has(key)).sort()
  const missingInZh = [...enKeys].filter((key) => !zhKeys.has(key)).sort()

  assert.deepEqual(missingInEn, [], `missing in en: ${missingInEn.join(', ')}`)
  assert.deepEqual(missingInZh, [], `missing in zh: ${missingInZh.join(', ')}`)
})

test('no translation is an empty string', () => {
  for (const [name, dictionary] of [['zh', zh], ['en', en]]) {
    for (const key of flatten(dictionary)) {
      const value = key.split('.').reduce((node, part) => node?.[part], dictionary)
      assert.equal(
        typeof value === 'string' && value.trim().length > 0,
        true,
        `${name}.${key} is empty`
      )
    }
  }
})
