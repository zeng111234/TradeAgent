/**
 * Tiny i18n layer -- no dependency.
 *
 * Why not vue-i18n: this app has two locales and a fixed string set; adding a
 * plugin (and its runtime) for that is more surface than the problem needs.
 * `t()` reads the `locale` ref during render, so every template that calls it
 * re-renders when the language changes -- no reload, no route dance.
 *
 * The dictionary is nested and addressed by dotted key: t('nav.console').
 * Missing keys fall back to Chinese, then to the key itself, which makes a
 * missed migration visible in the UI instead of silently blank.
 */
import { ref } from 'vue'
import zh from './zh.js'
import en from './en.js'

const STORAGE_KEY = 'tradeagent.locale'
const FALLBACK = 'zh'

const readStored = () => {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

export const locale = ref(readStored() === 'en' ? 'en' : FALLBACK)

const dictionaries = { zh, en }

const lookup = (dict, key) => key.split('.').reduce((node, part) => (
  node && typeof node === 'object' ? node[part] : undefined
), dict)

/**
 * Translate one key. `{name}` placeholders are filled from `params`.
 * Returning the key itself (rather than '') on a miss keeps a missing
 * translation visible in review instead of turning into a blank label.
 */
export function t(key, params) {
  const raw = lookup(dictionaries[locale.value], key)
    ?? lookup(dictionaries[FALLBACK], key)
    ?? key
  if (typeof raw !== 'string') return key
  if (!params) return raw
  return raw.replace(/\{(\w+)\}/g, (_, name) => (
    params[name] === undefined ? `{${name}}` : String(params[name])
  ))
}

export function setLocale(next) {
  locale.value = next === 'en' ? 'en' : 'zh'
  try {
    localStorage.setItem(STORAGE_KEY, locale.value)
    document.documentElement.lang = locale.value === 'zh' ? 'zh-CN' : 'en'
  } catch {
    /* private mode: the switch still works for this page */
  }
}

export function toggleLocale() {
  setLocale(locale.value === 'zh' ? 'en' : 'zh')
}

/** Shorthand for components that prefer `const { t } = useI18n()`. */
export function useI18n() {
  return { t, locale, setLocale, toggleLocale }
}
