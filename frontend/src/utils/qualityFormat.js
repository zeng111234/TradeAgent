/**
 * Formatting for the quality page -- kept out of the component so it can be
 * tested without a browser.
 *
 * The one rule every function here follows: **an unknown value is not a zero.**
 * A reply rate of `null` means "no delivered mail to reply to", and rendering
 * it as `0%` would say every recipient ignored us. The reports API already
 * returns `null` for exactly this reason (`app/services/quality_service.py`);
 * a formatter that turns it back into a number undoes that decision at the last
 * possible moment.
 */

/** Percentage, or an em dash when there is no sample. */
export function formatRate(value) {
  if (value === null || value === undefined) return '—'
  return `${value}%`
}

/**
 * Milliseconds to seconds for display.
 *
 * The API field is `elapsed_ms` and really is milliseconds: ``save_run`` writes
 * ``int(seconds * 1000)``. (The *SSE* field called ``elapsed`` is seconds --
 * different field, different unit, which is exactly the confusion R52 records.)
 */
export function formatSeconds(ms, digits = 1) {
  if (ms === null || ms === undefined) return '—'
  const seconds = ms / 1000
  if (seconds < 1) return `${seconds.toFixed(3)}s`
  return `${seconds.toFixed(digits)}s`
}

/** A count with its unit, so "3" is never ambiguous. */
export function formatCount(value, noun) {
  if (value === null || value === undefined) return '—'
  return `${value} ${noun}`
}

const KIND_LABELS = {
  answer_them: '回复对方',
  send_follow_up: '继续跟进',
  awaiting_approval: '等待审批',
  change_channel: '换渠道',
  fix_delivery: '修复投递',
  review: '归档复核',
  unattributed: '未标注来源',
}

/**
 * A human word for a planned action.
 *
 * An unrecognised kind is returned as-is rather than mapped to "其他": if the
 * backend grows a kind before this file knows about it, the raw value is
 * information and "other" is not.
 */
export function kindLabel(kind) {
  if (!kind) return '—'
  return KIND_LABELS[kind] || kind
}

/**
 * A human word for an attempt bucket.
 *
 * ``"unknown"`` is its own answer: the suggestion was recorded but the number
 * of previous tries was not, and calling that ``0`` would report first contacts
 * that never happened.
 */
export function attemptLabel(attempt) {
  if (attempt === null || attempt === undefined) return '—'
  const value = String(attempt)
  if (value === 'unknown') return '次数未知'
  if (value === '0') return '首次联系'
  if (value.endsWith('+')) return `第 ${value.slice(0, -1)} 次及以后`
  return `第 ${value} 次跟进`
}

/**
 * One line about how much of the mail can be attributed.
 *
 * Returns a sentence rather than a percentage because the percentage alone
 * ("coverage 0%") reads as a failure, when the usual cause is old mail that
 * predates the columns.
 */
export function coverageSentence(loop) {
  if (!loop || !loop.sent) {
    return '窗口内没有已投递的邮件，回复率暂时无法计算。'
  }
  if (loop.unattributed === 0) {
    return `全部 ${loop.sent} 封已投递邮件都带有建议来源，可以按建议类型比较回复率。`
  }
  return (
    `${loop.sent} 封已投递邮件中 ${loop.attributed} 封带有建议来源，` +
    `${loop.unattributed} 封没有（人工撰写、agent 草稿，或早于来源字段的旧邮件）。` +
    '下表的比例只统计能归因的部分，"未标注来源"单独成行。'
  )
}

/** Which daily column is worth showing, in priority order. */
export function dailyHasActivity(day) {
  return Boolean(day && (day.runs_total || day.jobs_total))
}
