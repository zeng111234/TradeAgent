<template>
  <div class="trace-diagram">
    <div v-if="error" class="diagram-error">
      <p><strong>Could not render the diagram.</strong></p>
      <p class="hint">{{ error }}</p>
      <pre class="source">{{ source }}</pre>
    </div>
    <div v-else ref="host" class="diagram-host" v-html="svg"></div>
    <details class="source-toggle">
      <summary>Mermaid source</summary>
      <pre class="source">{{ source }}</pre>
    </details>
  </div>
</template>

<script setup>
/**
 * Renders a Mermaid diagram string.
 *
 * Mermaid is imported lazily so it is only fetched when a trace is actually
 * viewed, and the library is loaded once per page rather than per component.
 * If rendering fails the raw source is shown instead of nothing — a diagram
 * that will not draw should still not hide the trace.
 */
import { onMounted, ref, watch } from 'vue'

const props = defineProps({
  source: { type: String, required: true },
})

const host = ref(null)
const svg = ref('')
const error = ref('')

let mermaidPromise = null

function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((mod) => {
      const mermaid = mod.default
      mermaid.initialize({
        startOnLoad: false,
        theme: 'neutral',
        securityLevel: 'strict',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      })
      return mermaid
    })
  }
  return mermaidPromise
}

async function render() {
  error.value = ''
  if (!props.source) {
    svg.value = ''
    return
  }
  try {
    const mermaid = await loadMermaid()
    // Unique id per render; Mermaid caches by id and would otherwise return
    // the previous diagram.
    const id = `trace-${Math.random().toString(36).slice(2, 10)}`
    const { svg: rendered } = await mermaid.render(id, props.source)
    svg.value = rendered
  } catch (err) {
    svg.value = ''
    error.value = err?.message || String(err)
  }
}

onMounted(render)
watch(() => props.source, render)
</script>

<style scoped>
.trace-diagram {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 12px;
  background: #fff;
}
.diagram-host {
  overflow-x: auto;
}
.diagram-error {
  color: #f56c6c;
  font-size: 13px;
}
.diagram-error .hint {
  color: #909399;
}
.source-toggle {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
}
.source {
  background: #f5f7fa;
  padding: 10px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 12px;
  line-height: 1.5;
  max-height: 320px;
}
</style>
