<template>
  <div v-if="(steps && steps.length) || live" class="timeline">
    <div v-for="step in steps" :key="step.round" class="round">
      <div class="round-head">
        <span class="round-no">{{ t('trace.round', { n: step.round }) }}</span>
        <span v-if="step.note" class="note">{{ step.note }}</span>
        <span v-if="step.prompt_tokens || step.completion_tokens" class="tokens">
          {{ t('trace.tokens', { prompt: compact(step.prompt_tokens), completion: compact(step.completion_tokens) }) }}
        </span>
        <span v-if="step.latency" class="latency">{{ step.latency }}s</span>
      </div>

      <!-- The reasoning channel, kept apart from the visible answer. Collapsed
           by default: it is evidence, not the answer, and a run with six rounds
           of thinking would otherwise push the conclusion off the screen. -->
      <details v-if="step.reasoning" class="reasoning">
        <summary>
          <span class="reasoning-label">{{ t('trace.reasoning') }}</span>
          <span class="reasoning-meta">{{ t('trace.reasoningMeta', { n: step.reasoning.length }) }}</span>
          <span v-if="step.reasoning_truncated" class="reasoning-meta">
            {{ t('trace.reasoningTruncated') }}
          </span>
        </summary>
        <div class="reasoning-body">{{ step.reasoning }}</div>
      </details>

      <p v-if="step.thought" class="thought">{{ step.thought }}</p>
      <p v-else-if="step.reasoning" class="thought muted">{{ t('trace.thoughtOnly') }}</p>
      <p v-else class="thought muted">{{ t('trace.thoughtEmpty') }}</p>

      <div v-if="!step.tool_calls || !step.tool_calls.length" class="no-call">
        {{ t('trace.noCall') }}
      </div>

      <div v-for="(call, index) in step.tool_calls" :key="`${step.round}-${index}`"
        class="call" :class="callClass(call)">
        <div class="call-head">
          <span class="tool">{{ call.name }}</span>
          <!-- A call the budget refused did not fail; painting it red says the
               tool broke. The two are different and the trace knows which. -->
          <el-tag v-if="call.refused_reason" type="warning" size="small" effect="plain">
            {{ t('trace.refused') }}
          </el-tag>
          <el-tag v-else :type="call.ok ? 'success' : 'danger'" size="small" effect="plain">
            {{ call.ok ? 'ok' : 'failed' }}
          </el-tag>
          <span v-if="call.refused_reason" class="refused-reason">
            {{ refusedReason(call.refused_reason) }}
          </span>
          <span class="latency">{{ call.elapsed }}s</span>
        </div>

        <p class="label">参数</p>
        <pre class="payload">{{ format(call.arguments) }}</pre>

        <p class="label">返回</p>
        <pre class="payload observation">{{ call.observation }}</pre>
      </div>
    </div>

    <!-- The round still in flight. `steps` only ever holds finished rounds:
         the loop writes one per round, so without this the console has nothing
         to show between "sent" and "round finished". -->
    <div v-if="live" class="round live">
      <div class="round-head">
        <span class="round-no">{{ t('trace.round', { n: live.round }) }}</span>
        <span class="live-dot"></span>
      </div>

      <details v-if="live.reasoning" class="reasoning" open>
        <summary>
          <span class="reasoning-label">{{ t('trace.reasoning') }}</span>
          <span class="reasoning-meta">{{ t('trace.reasoningMeta', { n: live.reasoning.length }) }}</span>
        </summary>
        <div class="reasoning-body">{{ live.reasoning }}</div>
      </details>

      <p v-if="live.content" class="thought">{{ live.content }}<span class="caret"></span></p>
    </div>
  </div>
  <el-empty v-else :description="t('trace.empty')" :image-size="60" />
</template>

<script setup>
/**
 * Round-by-round replay of an agent run.
 *
 * Observations contain raw scraped page text and full tool payloads, so they
 * are rendered inside <pre> with text interpolation only — never with v-html.
 * Long payloads are collapsed by CSS rather than truncated, so nothing is
 * silently lost.
 *
 * `live` is the round currently being generated. It is deliberately separate
 * from `steps`: the server persists a round only when it completes, so mixing
 * a partial round into `steps` would make the next snapshot overwrite it with
 * a shorter version.
 */
import { t } from '../i18n/index.js'

defineProps({
  steps: { type: Array, default: () => [] },
  live: { type: Object, default: null },
})

function format(value) {
  if (value === null || value === undefined) return '(none)'
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function compact(value) {
  const n = Number(value) || 0
  if (n < 1000) return String(n)
  const k = n / 1000
  return `${k >= 10 ? Math.round(k) : k.toFixed(1)}k`
}

function callClass(call) {
  if (call.refused_reason) return 'refused'
  return call.ok ? 'ok' : 'bad'
}

function refusedReason(reason) {
  const key = `trace.refusedReason.${reason}`
  const label = t(key)
  // A code the dictionary does not know yet is shown as the code, not as a
  // missing-key string: an unstyled word is more honest than a broken one.
  return label === key ? reason : label
}
</script>

<style scoped>
.timeline {
  margin-top: 16px;
}
.round {
  border-left: 3px solid var(--border);
  padding: 0 0 16px 16px;
  margin-bottom: 16px;
}
.round.live {
  border-left-color: var(--accent);
  padding-bottom: 4px;
  margin-bottom: 0;
}
.round-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 6px;
}
.round-no {
  font-weight: 700;
  font-size: 12px;
  letter-spacing: 0.05em;
  color: var(--text-2);
  text-transform: uppercase;
}
.note {
  font-size: 12px;
  color: var(--warning);
}
.tokens {
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
.latency {
  font-size: 12px;
  color: var(--text-3);
  margin-left: auto;
}
.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.25; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1); }
}
.caret {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 2px;
  vertical-align: -0.15em;
  background: var(--accent);
  animation: pulse 1s steps(2, start) infinite;
}

/* ----------------------------------------------------------- reasoning -- */
.reasoning {
  margin: 0 0 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--bg-subtle);
}
.reasoning > summary {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  cursor: pointer;
  list-style: none;
  font-size: 12px;
  color: var(--text-2);
  user-select: none;
}
.reasoning > summary::-webkit-details-marker {
  display: none;
}
.reasoning > summary::before {
  content: '▸';
  font-size: 10px;
  color: var(--text-3);
  transition: transform 0.15s ease;
}
.reasoning[open] > summary::before {
  transform: rotate(90deg);
}
.reasoning-label {
  font-weight: 600;
}
.reasoning-meta {
  font-size: 11px;
  color: var(--text-3);
}
.reasoning-body {
  padding: 0 12px 10px;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow: auto;
  border-top: 1px dashed var(--border-subtle);
  padding-top: 8px;
}

/* --------------------------------------------------------------- text --- */
.thought {
  font-size: 14px;
  line-height: 1.6;
  color: var(--text-1);
  margin: 0 0 10px;
  white-space: pre-wrap;
  word-break: break-word;
}
.thought.muted {
  color: var(--text-disabled);
  font-style: italic;
}
.no-call {
  font-size: 13px;
  color: var(--text-3);
  font-style: italic;
}

/* --------------------------------------------------------------- calls -- */
.call {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin: 10px 0;
  background: var(--bg-subtle);
}
.call.bad {
  border-color: var(--negative-border);
  background: var(--negative-weak);
}
.call.refused {
  border-color: var(--warning-border, var(--border));
}
.call-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.tool {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 700;
  font-size: 13px;
  color: var(--accent);
}
.refused-reason {
  font-size: 12px;
  color: var(--text-3);
}
.label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-3);
  margin: 8px 0 2px;
}
.payload {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  padding: 8px;
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow: auto;
}
.observation {
  color: var(--text-2);
}
</style>
