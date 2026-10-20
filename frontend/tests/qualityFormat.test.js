import test from 'node:test'
import assert from 'node:assert/strict'

import {
  attemptLabel,
  coverageSentence,
  dailyHasActivity,
  formatCount,
  formatRate,
  formatSeconds,
  kindLabel,
} from '../src/utils/qualityFormat.js'

test('an unknown rate renders as a dash, never as zero', () => {
  assert.equal(formatRate(null), '—')
  assert.equal(formatRate(undefined), '—')
  assert.equal(formatRate(0), '0%')
  assert.equal(formatRate(42.9), '42.9%')
})

test('elapsed is milliseconds and is displayed as seconds', () => {
  assert.equal(formatSeconds(null), '—')
  assert.equal(formatSeconds(6000), '6.0s')
  assert.equal(formatSeconds(12000), '12.0s')
  // Sub-second runs keep their precision rather than rounding to 0.0s.
  assert.equal(formatSeconds(12.5), '0.013s')
})

test('a count always carries its unit', () => {
  assert.equal(formatCount(0, '封'), '0 封')
  assert.equal(formatCount(null, '封'), '—')
})

test('a known kind is translated and an unknown one is shown as-is', () => {
  assert.equal(kindLabel('send_follow_up'), '继续跟进')
  assert.equal(kindLabel('unattributed'), '未标注来源')
  // A kind this file has not seen yet is information, not "other".
  assert.equal(kindLabel('escalate_to_human'), 'escalate_to_human')
  assert.equal(kindLabel(null), '—')
})

test('attempt buckets say what they mean', () => {
  assert.equal(attemptLabel('0'), '首次联系')
  assert.equal(attemptLabel('2'), '第 2 次跟进')
  assert.equal(attemptLabel('5+'), '第 5 次及以后')
  assert.equal(attemptLabel('unknown'), '次数未知')
  assert.equal(attemptLabel(null), '—')
})

test('coverage distinguishes no mail from unattributed mail', () => {
  const empty = coverageSentence({ sent: 0, attributed: 0, unattributed: 0 })
  assert.match(empty, /没有已投递的邮件/)

  const complete = coverageSentence({ sent: 3, attributed: 3, unattributed: 0 })
  assert.match(complete, /全部 3 封/)

  const partial = coverageSentence({ sent: 4, attributed: 3, unattributed: 1 })
  assert.match(partial, /4 封已投递邮件中 3 封/)
  assert.match(partial, /1 封没有/)
})

test('a zero day is not activity', () => {
  assert.equal(dailyHasActivity({ runs_total: 0, jobs_total: 0 }), false)
  assert.equal(dailyHasActivity({ runs_total: 1, jobs_total: 0 }), true)
  assert.equal(dailyHasActivity(null), false)
})
