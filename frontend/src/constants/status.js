/**
 * Status semantics, in one place.
 *
 * Before this file, each view carried its own private map and the same word
 * meant a different colour depending on which page you were on: `pending` was
 * grey-blue in the email table and orange on the pending-review cards, `running`
 * was info on Quality and a hard-coded warning on Runs, and a low match score
 * was grey / orange / red on three different pages. A tag type is an argument
 * to el-tag -- 'primary' | 'success' | 'warning' | 'danger' | 'info' -- so the
 * vocabulary below names the *meaning* first and resolves to a tag type once.
 *
 *   accent   -> 'primary'   the one thing in motion or selected
 *   positive -> 'success'   finished, and the outcome was good
 *   warning  -> 'warning'   needs attention, not an error
 *   negative -> 'danger'    failed
 *   neutral  -> 'info'      a fact, not a verdict
 *
 * Note on el-tag: a type of '' is *not* neutral -- Element Plus falls through
 * `type || "primary"` and paints it as the accent colour. Neutral is 'info'
 * explicitly. That fall-through is half of why the old maps disagreed.
 *
 * Scores are separate: el-progress takes a hex value or a stops array, not a
 * tag type, so scoreColor resolves through theme.js instead. Low score is
 * neutral, not red -- "not relevant" is not "wrong".
 */
import { accent, positive, warning, negative, neutral } from './theme.js'
import { t as translate } from '../i18n/index.js'

const ACCENT = 'primary'
const POSITIVE = 'success'
const WARNING = 'warning'
const NEGATIVE = 'danger'
const NEUTRAL = 'info'

/** customer.stage -- an order pipeline, so the late stages earn a colour. */
const STAGE_TYPES = Object.freeze({
  new: NEUTRAL,
  contacted: NEUTRAL,
  interested: ACCENT,
  quoting: ACCENT,
  sample: WARNING,
  ordering: POSITIVE,
  completed: POSITIVE,
  lost: NEUTRAL,
})

/**
 * Enum label through the dictionaries.
 *
 * The raw value is the API contract; the dictionary is the display form, and
 * it is read on every render, so switching language re-labels every tag.
 * Data drifts (both `follow_up` and `FOLLOW_UP` are stored), so the key is
 * lowercased first. `t()` returns the key itself on a miss -- for an unknown
 * enum value the caller wants the raw value, not "status.task.foo", so a miss
 * is detected and the value passed through.
 */
const enumLabel = (group, value) => {
  const key = String(value || '').toLowerCase()
  if (!key) return ''
  const full = `status.${group}.${key}`
  const translated = translate(full)
  return translated === full ? String(value) : translated
}

export const stageLabel = (stage) => enumLabel('stage', stage)
export const runStatusLabel = (status) => enumLabel('run', status)
export const emailStatusLabel = (status) => enumLabel('email', status)
export const taskStatusLabel = (status) => enumLabel('task', status)
export const priorityLabel = (priority) => enumLabel('priority', priority)
export const taskTypeLabel = (value) => enumLabel('taskType', value)
export const noteTypeLabel = (value) => enumLabel('note', value)
export const templateCategoryLabel = (category) => enumLabel('templateCategory', category)
export const riskLabel = (level) => enumLabel('risk', level)

export const stageTagType = (stage) => STAGE_TYPES[stage] || NEUTRAL

/**
 * run.status -- same vocabulary for the history table and the Quality page.
 * `starting` and `running` are both "in flight": accent, because that is the
 * one state a reader is watching change. `truncated` is not a failure (the run
 * hit its budget and stopped with a result), so it is warning, like `stalled`.
 */
const RUN_TYPES = Object.freeze({
  succeeded: POSITIVE,
  truncated: WARNING,
  stalled: WARNING,
  running: ACCENT,
  starting: ACCENT,
  cancelling: ACCENT,
  // Stopped on purpose is a fact, not a failure. Painting it red would make a
  // deliberate action look like a defect in the run.
  cancelled: NEUTRAL,
  error: NEGATIVE,
})

export const runStatusType = (status) => RUN_TYPES[status] || NEUTRAL

/** email.status -- pending is an action waiting on a human, not a neutral fact. */
const EMAIL_TYPES = Object.freeze({
  pending: WARNING,
  sent: POSITIVE,
  delivered: POSITIVE,
  opened: POSITIVE,
  replied: ACCENT,
  failed: NEGATIVE,
  bounced: NEGATIVE,
})

export const emailStatusType = (status) => EMAIL_TYPES[status] || NEUTRAL

/** task.status */
const TASK_TYPES = Object.freeze({
  pending: WARNING,
  in_progress: ACCENT,
  completed: POSITIVE,
  cancelled: NEUTRAL,
})

export const taskStatusType = (status) => TASK_TYPES[status] || NEUTRAL

const PRIORITY_TYPES = Object.freeze({
  urgent: NEGATIVE,
  high: WARNING,
  // Medium used to be `''`, which Element Plus paints as the accent colour by
  // accident. Priority is a status label, and the accent is never a status
  // colour (see tokens.css) -- quiet it down; the word "medium" still carries
  // the distinction from "low".
  medium: NEUTRAL,
  low: NEUTRAL,
})

export const priorityType = (priority) => PRIORITY_TYPES[priority] || NEUTRAL

/**
 * Match score -> el-progress colour. Thresholds are 70 / 40; CustomerDetail's
 * old stops array used 30 / 60 and Agent used 70 / 50, so the same number
 * turned up in two colours on two pages. One rule now, shared by the hex form
 * (progress strokes), the tag form (table cells) and the class form.
 */
const scoreBand = (score) => (score >= 70 ? 'high' : score >= 40 ? 'mid' : 'low')

export const scoreColor = (score) =>
  ({ high: accent, mid: warning, low: neutral })[scoreBand(score)]

export const scoreTagType = (score) =>
  ({ high: ACCENT, mid: WARNING, low: NEUTRAL })[scoreBand(score)]

/** Same bands as a class name, for cells that colour text without a tag. */
export const scoreClass = (score) => `score-${scoreBand(score)}`

/**
 * Card-edge form of the score bands (the left edge of a scanned-lead card).
 * High keeps green -- a strong match reads as good news -- but the thresholds
 * follow scoreBand's 70/40; this page used to cut at 70/50, so a 45 picked up
 * an edge colour here and a different one in the progress bar beside it.
 */
export const scoreEdgeClass = (score) =>
  ({ high: 'edge-positive', mid: 'edge-warning', low: '' })[scoreBand(score)]

/** Customer note types -- a category, so: quiet by default, loud only for risk. */
const NOTE_TYPES = Object.freeze({
  general: NEUTRAL,
  email: ACCENT,
  call: POSITIVE,
  meeting: WARNING,
  // Was NEGATIVE -- a quotation is a pricing note, not a red alert. None of
  // these categories is a risk, and a red tag on the timeline reads as one.
  quotation: NEUTRAL,
})

export const noteTypeColor = (type) => NOTE_TYPES[type] || NEUTRAL

/** Agent-page classifiers. These label *content*, so they stay compact. */
const INTENT_TYPES = Object.freeze({
  inquiry: POSITIVE,
  price_negotiation: WARNING,
  sample_request: ACCENT,
  rejection: NEGATIVE,
  order: POSITIVE,
  follow_up: NEUTRAL,
})

export const intentColor = (intent) => INTENT_TYPES[intent] || NEUTRAL

const SENTIMENT_TYPES = Object.freeze({
  positive: POSITIVE,
  neutral: NEUTRAL,
  negative: NEGATIVE,
})

export const sentimentColor = (s) => SENTIMENT_TYPES[s] || NEUTRAL

const URGENCY_TYPES = Object.freeze({
  high: NEGATIVE,
  medium: WARNING,
  low: NEUTRAL,
  not_assessed: NEUTRAL,
})

export const urgencyColor = (u) => URGENCY_TYPES[u] || NEUTRAL

/**
 * Daily follow-up priority -- "how loudly does this customer need me today".
 * Not task PRIORITY_TYPES (there `high` is only a warning): a follow-up the
 * agent flags as high is the red one. Both the tag and the card edge read it.
 */
const FOLLOWUP_TYPES = Object.freeze({ high: NEGATIVE, medium: WARNING })

export const followupType = (p) => FOLLOWUP_TYPES[p] || NEUTRAL

export const followupEdgeClass = (p) =>
  ({ high: 'edge-negative', medium: 'edge-warning' })[p] || ''

/** Churn risk level: how close a customer is to leaving. */
const RISK_TYPES = Object.freeze({
  critical: NEGATIVE,
  high: WARNING,
  medium: NEUTRAL,
})

export const riskTagType = (level) => RISK_TYPES[level] || NEUTRAL

/** Card-edge form -- same bands as the tag. */
export const riskEdgeClass = (level) =>
  ({ critical: 'edge-negative', high: 'edge-warning', medium: 'edge-neutral' })[level] || ''
