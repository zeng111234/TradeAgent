<template>
  <div class="trace-diagram">
    <div v-if="error" class="diagram-error">
      <p><strong>Could not render the diagram.</strong></p>
      <p class="hint">{{ error }}</p>
      <pre class="source">{{ source }}</pre>
    </div>

    <template v-else>
      <div class="toolbar">
        <el-button-group size="small">
          <el-button :disabled="zoom <= MIN" title="Zoom out" @click="zoomBy(-0.25)">
            <el-icon><ZoomOut /></el-icon>
          </el-button>
          <el-button :disabled="zoom >= MAX" title="Zoom in" @click="zoomBy(0.25)">
            <el-icon><ZoomIn /></el-icon>
          </el-button>
        </el-button-group>
        <el-button size="small" :disabled="zoom === 1" @click="reset">
          {{ Math.round(zoom * 100) }}%
        </el-button>
        <el-button size="small" @click="fit">适应宽度</el-button>
        <el-button size="small" @click="toggleFullscreen">
          <el-icon><FullScreen /></el-icon>
          {{ fullscreen ? '退出全屏' : '全屏' }}
        </el-button>
        <span class="hint">也可用 Ctrl + 滚轮缩放，拖动平移</span>
      </div>

      <div
        ref="viewport"
        class="diagram-viewport"
        :class="{ fullscreen }"
        :style="fullscreen ? { height: viewportHeight } : null"
        @mousedown="startDrag"
        @mousemove="onDrag"
        @mouseup="endDrag"
        @mouseleave="endDrag"
        @wheel.ctrl.prevent="onWheel"
      >
        <div class="diagram-canvas" :style="canvasStyle">
          <div ref="host" class="diagram-host" v-html="svg"></div>
        </div>
      </div>
    </template>

    <details class="source-toggle">
      <summary>Mermaid source</summary>
      <pre class="source">{{ source }}</pre>
    </details>
  </div>
</template>

<script setup>
/**
 * Renders a Mermaid diagram with zoom and pan.
 *
 * Mermaid can supply its own pan/zoom, but only when `securityLevel` is relaxed
 * to `loose` — which also turns on `htmlLabels` and weakens the sanitiser. Trace
 * labels contain scraped page text from the open web, so that trade is not worth
 * making. These controls are ours: the SVG is left exactly as Mermaid rendered
 * it and only CSS transform changes, so sanitisation stays strict.
 *
 * Zoom scales by widening the inline-block canvas and transforming its content.
 * The element box grows with the scale, which is what gives the viewport a real
 * scroll range instead of clipping the enlarged diagram.
 *
 * Mermaid is imported lazily so it is fetched only when a trace is actually
 * viewed, and loaded once per page rather than per component.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  source: { type: String, required: true },
  // Height of the fullscreen overlay; passed in so the component does not have
  // to guess the surrounding chrome.
  fullscreenHeight: { type: String, default: 'calc(100vh - 120px)' },
})

const MIN = 0.25
const MAX = 4
// A diagram narrower than this is scaled to fill the viewport; a wider one keeps
// its natural size and scrolls. Squeezing a ten-participant sequence diagram into
// a 900px card makes every label unreadable, which is the problem the zoom
// controls exist to solve.
const READABLE_MIN_WIDTH = 900

const host = ref(null)
const viewport = ref(null)
const svg = ref('')
const error = ref('')

const zoom = ref(1)
const dragging = ref(false)
const fullscreen = ref(false)
const baseWidth = ref(0)
const viewportHeight = computed(() => props.fullscreenHeight)

let mermaidPromise = null

function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((mod) => {
      const mermaid = mod.default
      mermaid.initialize({
        startOnLoad: false,
        theme: 'neutral',
        // Kept strict on purpose — see the component comment.
        securityLevel: 'strict',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
        // Let Mermaid size to the diagram instead of squeezing it to its parent;
        // the viewport below provides scrolling.
        sequence: { useMaxWidth: false },
        flowchart: { useMaxWidth: false },
      })
      return mermaid
    })
  }
  return mermaidPromise
}

const canvasStyle = computed(() => ({
  transform: `scale(${zoom.value})`,
  transformOrigin: '0 0',
  width: baseWidth.value ? `${baseWidth.value}px` : undefined,
  cursor: dragging.value ? 'grabbing' : zoom.value > 1 ? 'grab' : 'default',
}))

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
    await nextTick()
    measure()
  } catch (err) {
    svg.value = ''
    error.value = err?.message || String(err)
  }
}

/**
 * Decide the diagram's rendered width once, from the SVG's own geometry.
 *
 * Read from `viewBox` rather than `getBoundingClientRect`, because the element
 * has already been scaled by the time this runs and a measured box would fold
 * the current zoom into the base.
 */
function measure() {
  const node = host.value?.querySelector('svg')
  if (!node) return
  node.style.maxWidth = 'none'
  node.style.width = '100%'
  node.style.height = 'auto'

  const viewBox = node.getAttribute('viewBox')
  const natural = viewBox ? Number(viewBox.split(/[\s,]+/)[2]) : 0
  const available = viewport.value?.clientWidth || READABLE_MIN_WIDTH
  const target = Number.isFinite(natural) && natural > 0 ? natural : available

  baseWidth.value = Math.round(Math.max(available, Math.min(target, READABLE_MIN_WIDTH * 4)))
}

function zoomBy(delta) {
  const next = Math.min(MAX, Math.max(MIN, Number((zoom.value + delta).toFixed(2))))
  if (next === zoom.value) return
  keepCentre(() => {
    zoom.value = next
  })
}

function reset() {
  keepCentre(() => {
    zoom.value = 1
  })
}

function fit() {
  reset()
  if (viewport.value) {
    viewport.value.scrollLeft = 0
    viewport.value.scrollTop = 0
  }
}

/** Change the zoom while holding the centre of the viewport steady. */
function keepCentre(mutate) {
  const box = viewport.value
  const centre = box
    ? { x: box.scrollLeft + box.clientWidth / 2, y: box.scrollTop + box.clientHeight / 2 }
    : null
  const wasZoom = zoom.value
  mutate()
  if (!box || !centre) return
  nextTick(() => {
    const ratio = zoom.value / wasZoom
    box.scrollLeft = centre.x * ratio - box.clientWidth / 2
    box.scrollTop = centre.y * ratio - box.clientHeight / 2
  })
}

let drag = null

function startDrag(event) {
  if (zoom.value <= 1 || event.button !== 0 || !viewport.value) return
  drag = {
    x: event.clientX,
    y: event.clientY,
    left: viewport.value.scrollLeft,
    top: viewport.value.scrollTop,
  }
  dragging.value = true
}

function onDrag(event) {
  if (!drag || !viewport.value) return
  viewport.value.scrollLeft = drag.left - (event.clientX - drag.x)
  viewport.value.scrollTop = drag.top - (event.clientY - drag.y)
}

function endDrag() {
  drag = null
  dragging.value = false
}

function onWheel(event) {
  zoomBy(event.deltaY < 0 ? 0.25 : -0.25)
}

function toggleFullscreen() {
  fullscreen.value = !fullscreen.value
  // Lock the page behind the overlay.
  document.body.style.overflow = fullscreen.value ? 'hidden' : ''
  nextTick(measure)
}

function onKeydown(event) {
  if (event.key === 'Escape' && fullscreen.value) toggleFullscreen()
}

let resizeTimer = null
function onResize() {
  clearTimeout(resizeTimer)
  resizeTimer = setTimeout(measure, 200)
}

onMounted(() => {
  render()
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', onResize)
  document.body.style.overflow = ''
})

watch(() => props.source, render)
</script>

<style scoped>
.trace-diagram {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  background: var(--bg-card);
}
.toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.toolbar .hint {
  color: var(--text-3);
  font-size: 12px;
  margin: 0;
}
.diagram-viewport {
  overflow: auto;
  max-height: 460px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  padding: 8px;
}
.diagram-viewport.fullscreen {
  position: fixed;
  inset: 24px;
  z-index: 3000;
  max-height: none;
  background: var(--bg-card);
  border: 1px solid var(--border);
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.22);
  padding: 16px;
}
.diagram-canvas {
  display: inline-block;
  min-width: 100%;
  transition: transform 0.12s ease-out;
}
.diagram-host :deep(svg) {
  display: block;
  max-width: none !important;
  height: auto;
}
.diagram-error {
  color: var(--negative);
  font-size: 13px;
}
.diagram-error .hint {
  color: var(--text-3);
}
.source-toggle {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-3);
}
.source {
  background: var(--bg-subtle);
  padding: 10px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 12px;
  line-height: 1.5;
  max-height: 320px;
}
</style>
