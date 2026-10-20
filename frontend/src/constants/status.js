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

const STAGE_LABELS = Object.freeze({
  new: 'New',
  contacted: 'Contacted',
  interested: 'Interested',
  quoting: 'Quoting',
  sample: 'Sample',
  ordering: 'Ordering',
  completed: 'Completed',
  lost: 'Lost',
})

export const stageTagType = (stage) => STAGE_TYPES[stage] || NEUTRAL
export const stageLabel = (stage) => STAGE_LABELS[stage] || stage

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
 * turned up in two colours on two pages. One rule now.
 */
export const scoreColor = (score) =>
  score >= 70 ? accent : score >= 40 ? warning : neutral

/** Customer note types -- a category, so: quiet by default, loud only for risk. */
const NOTE_TYPES = Object.freeze({
  general: NEUTRAL,
  email: ACCENT,
  call: POSITIVE,
  meeting: WARNING,
  quotation: NEGATIVE,
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
