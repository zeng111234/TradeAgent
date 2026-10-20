<template>
  <div v-if="steps && steps.length" class="timeline">
    <div v-for="step in steps" :key="step.round" class="round">
      <div class="round-head">
        <span class="round-no">第 {{ step.round }} 轮</span>
        <span v-if="step.note" class="note">{{ step.note }}</span>
        <span v-if="step.latency" class="latency">{{ step.latency }}s</span>
      </div>

      <p v-if="step.thought" class="thought">{{ step.thought }}</p>
      <p v-else class="thought muted">（本轮没有输出文本）</p>

      <div v-if="!step.tool_calls || !step.tool_calls.length" class="no-call">
        未调用工具
      </div>

      <div v-for="(call, index) in step.tool_calls" :key="`${step.round}-${index}`"
        class="call" :class="call.ok ? 'ok' : 'bad'">
        <div class="call-head">
          <span class="tool">{{ call.name }}</span>
          <el-tag :type="call.ok ? 'success' : 'danger'" size="small" effect="plain">
            {{ call.ok ? 'ok' : 'failed' }}
          </el-tag>
          <span class="latency">{{ call.elapsed }}s</span>
        </div>

        <p class="label">参数</p>
        <pre class="payload">{{ format(call.arguments) }}</pre>

        <p class="label">返回</p>
        <pre class="payload observation">{{ call.observation }}</pre>
      </div>
    </div>
  </div>
  <el-empty v-else description="没有可显示的轮次" :image-size="60" />
</template>

<script setup>
/**
 * Round-by-round replay of an agent run.
 *
 * Observations contain raw scraped page text and full tool payloads, so they
 * are rendered inside <pre> with text interpolation only — never with v-html.
 * Long payloads are collapsed by CSS rather than truncated, so nothing is
 * silently lost.
 */
defineProps({
  steps: { type: Array, default: () => [] },
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
.latency {
  font-size: 12px;
  color: var(--text-3);
  margin-left: auto;
}
.thought {
  font-size: 14px;
  line-height: 1.6;
  color: var(--text-1);
  margin: 0 0 10px;
  white-space: pre-wrap;
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
.call-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.tool {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 700;
  font-size: 13px;
  color: var(--accent);
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
