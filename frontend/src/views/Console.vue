<template>
  <div class="console-page">
    <div ref="scroller" class="console-scroll">
      <!-- Empty state: the product introduces itself as an agent, not a form. -->
      <section v-if="!turns.length" class="console-empty">
        <span class="agent-mark"><el-icon><MagicStick /></el-icon></span>
        <h1>{{ t('console.greeting') }}</h1>
        <p class="console-intro">{{ t('console.intro') }}</p>

        <el-alert
          v-if="capabilities.configured === false"
          type="warning"
          :closable="false"
          show-icon
          :title="t('console.notConfigured')"
          :description="t('console.notConfiguredHint')"
          class="mb-5"
        />

        <p class="suggestions-title">{{ t('console.suggestionsTitle') }}</p>
        <div class="suggestions">
          <button
            v-for="(s, i) in suggestions"
            :key="i"
            type="button"
            @click="send(s)"
          >{{ s }}</button>
        </div>

        <div v-if="recentRuns.length" class="recent">
          <p class="suggestions-title">{{ t('console.history') }}</p>
          <router-link
            v-for="run in recentRuns"
            :key="run.run_id"
            to="/runs"
            class="recent-row"
          >
            <span class="recent-goal">{{ run.goal }}</span>
            <el-tag :type="statusType(run.status)" size="small" effect="plain">{{ runLabel(run.status) }}</el-tag>
          </router-link>
        </div>
      </section>

      <!-- Thread: one goal, one answer, with the tool calls in between. -->
      <div v-else class="thread">
        <article v-for="turn in turns" :key="turn.id" class="turn">
          <div class="msg-user">
            <p>{{ turn.goal }}</p>
          </div>

          <div class="msg-agent">
            <div class="agent-head">
              <span class="agent-mark agent-mark--sm"><el-icon><MagicStick /></el-icon></span>
              <el-tag :type="statusType(turn.status)" size="small" effect="plain">
                {{ runLabel(turn.status) }}
              </el-tag>
              <span v-if="turn.rounds || turn.status !== 'starting'" class="agent-meta">
                {{ t('console.metaRounds', { n: turn.rounds }) }}
                · {{ t('console.metaCalls', { n: turn.calls }) }}
                · {{ t('console.metaElapsed', { n: elapsed(turn) }) }}
              </span>
            </div>

            <p v-if="turn.warning" class="agent-warning">{{ turn.warning }}</p>

            <!-- Live: steps appear as rounds land, then collapse into a toggle. -->
            <div v-if="isRunning(turn)" class="thinking">
              <p class="thinking-line">
                <span class="pulse"></span>
                {{ turn.steps.length ? t('console.running') : t('console.waitingFirstRound') }}
              </p>
              <TraceTimeline v-if="turn.steps.length" :steps="turn.steps" />
            </div>

            <template v-else>
              <div v-if="turn.steps.length" class="trace-block">
                <button type="button" class="trace-toggle" @click="turn.showTrace = !turn.showTrace">
                  <el-icon><ArrowRight /></el-icon>
                  {{ t('console.metaCalls', { n: turn.calls }) }}
                  <span class="trace-hint">{{ turn.showTrace ? '−' : '+' }}</span>
                </button>
                <TraceTimeline v-if="turn.showTrace" :steps="turn.steps" />
              </div>

              <div v-if="turn.result?.final_output" class="conclusion">
                <div class="md" v-html="renderMarkdown(turn.result.final_output)"></div>
              </div>
              <p v-else-if="turn.error" class="agent-warning">{{ turn.error }}</p>

              <div class="agent-foot">
                <a v-if="turn.runId" :href="traceUrl(turn.runId)" target="_blank" rel="noopener">
                  {{ t('console.openTrace') }}
                </a>
                <button type="button" @click="reset">{{ t('console.newChat') }}</button>
              </div>
            </template>
          </div>
        </article>
      </div>
    </div>

    <!-- Composer -->
    <div class="composer">
      <div class="composer-box">
        <el-input
          v-model="goal"
          type="textarea"
          resize="none"
          :autosize="{ minRows: 1, maxRows: 5 }"
          :placeholder="t('console.placeholder')"
          :disabled="running"
          @keydown.enter.exact.prevent="send()"
        />
        <el-button
          type="primary"
          :loading="running"
          :disabled="!goal.trim() || capabilities.configured === false"
          @click="send()"
        >{{ running ? t('console.sending') : t('console.send') }}</el-button>
      </div>
      <p class="composer-hint">Enter ↵ · Shift+Enter</p>
    </div>
  </div>
</template>

<script setup>
/**
 * The agent console -- the front door.
 *
 * One goal per turn: the user's words, the live tool trace while the loop runs,
 * then the answer. The run itself is the same server-side loop the Runs page
 * drives; this page follows the same SSE stream (with the same polling
 * fallback) and only changes how it is presented.
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { runApi } from '../api'
import { runStatusType as statusType } from '../constants/status.js'
import { renderMarkdown } from '../utils/markdown'
import { isTerminal, mergeSteps, normaliseRun } from '../utils/runProgress'
import { readEventStream, reconnectDelay } from '../utils/sse'
import TraceTimeline from '../components/TraceTimeline.vue'
import { t } from '../i18n/index.js'

const MAX_STREAM_ATTEMPTS = 3
const POLL_INTERVAL_MS = 1200

const route = useRoute()
const router = useRouter()

const capabilities = ref({})
const turns = ref([])
const goal = ref('')
const recentRuns = ref([])
const scroller = ref(null)

let streamAbort = null
let pollTimer = null
let clockTimer = null
let turnSeq = 0

const running = computed(() => turns.value.some((turn) => isRunning(turn)))

const suggestions = computed(() => [
  t('console.suggestion1'),
  t('console.suggestion2'),
  t('console.suggestion3'),
])

const isRunning = (turn) => turn.status === 'starting' || turn.status === 'running'
const runLabel = (status) => t(`status.run.${status}`)
const traceUrl = (runId) => runApi.traceUrl(runId)
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

const elapsed = (turn) => Math.max(0, Math.round(((turn.endedAt || Date.now()) - turn.startedAt) / 1000))

function scrollToBottom() {
  nextTick(() => {
    const el = scroller.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function newTurn(text) {
  // Reactive object, not a plain one pushed into the array: every mutation
  // below happens through this reference, and a raw object inside a ref does
  // not notify the view (the turn would sit at "starting" forever).
  const turn = reactive({
    id: ++turnSeq,
    goal: text,
    runId: null,
    status: 'starting',
    startedAt: Date.now(),
    endedAt: 0,
    rounds: 0,
    calls: 0,
    steps: [],
    warning: '',
    error: '',
    result: null,
    finished: false,
    showTrace: false,
  })
  turns.value.push(turn)
  return turn
}

function applySnapshot(turn, snapshot) {
  turn.status = snapshot.status || turn.status
  turn.rounds = snapshot.rounds ?? turn.rounds
  turn.calls = snapshot.tool_calls ?? turn.calls
  turn.steps = mergeSteps(turn.steps, snapshot.steps || [])
  if (isTerminal(turn.status)) finish(turn, snapshot)
  scrollToBottom()
}

function finish(turn, snapshot) {
  // Idempotent by flag, not by status: callers set the status before calling
  // this, so a "still running?" guard would refuse the very run it is ending.
  if (turn.finished) return
  turn.finished = true
  stopTimers()
  turn.endedAt = Date.now()
  turn.status = snapshot.status || turn.status
  // The final snapshot carries the same counters the live frames did; a
  // synchronous answer arrives with no frames at all, so absorb them here.
  turn.rounds = snapshot.rounds ?? turn.rounds
  turn.calls = snapshot.tool_calls ?? turn.calls
  turn.steps = mergeSteps(turn.steps, snapshot.steps || [])
  turn.result = normaliseRun(snapshot)
  if (turn.status === 'error') turn.error = turn.result.stop_reason || t('console.failed')
  scrollToBottom()
}

function fail(turn, message) {
  stopTimers()
  turn.endedAt = Date.now()
  turn.status = 'error'
  turn.error = message
  scrollToBottom()
}

function startClock() {
  stopClock()
  clockTimer = setInterval(() => {
    // Touch the elapsed values by re-rendering: the refs above are read in the
    // template, so bumping a counter is enough.
    if (turns.value.some(isRunning)) turns.value = [...turns.value]
  }, 1000)
}

function stopClock() {
  if (clockTimer) {
    clearInterval(clockTimer)
    clockTimer = null
  }
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function abortStream() {
  if (streamAbort) {
    streamAbort.abort()
    streamAbort = null
  }
}

function stopTimers() {
  abortStream()
  stopPolling()
  stopClock()
}

async function pollUntilDone(turn, runId) {
  if (!isRunning(turn)) return
  try {
    const snapshot = await runApi.get(runId)
    applySnapshot(turn, snapshot)
    if (!isRunning(turn)) return
    pollTimer = setTimeout(() => pollUntilDone(turn, runId), POLL_INTERVAL_MS)
  } catch {
    turn.warning = t('console.failed')
    turn.status = 'error'
    stopTimers()
  }
}

/** Same stream contract as the Runs page: one SSE connection, resumable by
 *  round id, falling back to polling when the path will not hold it open. */
async function follow(turn, runId) {
  let attempt = 0
  let lastId = 0

  while (isRunning(turn) && turn.runId === runId) {
    const controller = new AbortController()
    streamAbort = controller
    try {
      const response = await fetch(runApi.eventsUrl(runId, lastId), {
        signal: controller.signal,
        headers: { Accept: 'text/event-stream' },
      })
      if (response.status === 404) {
        fail(turn, t('console.failed'))
        return
      }
      if (!response.ok) throw new Error(`stream answered ${response.status}`)

      attempt = 0
      await readEventStream(response, (frame) => {
        if (Number.isFinite(frame.id)) lastId = Math.max(lastId, frame.id)
        let payload
        try {
          payload = JSON.parse(frame.data)
        } catch {
          return
        }
        if (frame.event === 'error') {
          fail(turn, payload.message || t('console.failed'))
          return
        }
        applySnapshot(turn, payload)
      })

      if (isRunning(turn)) await sleep(reconnectDelay(++attempt))
    } catch (err) {
      if (err?.name === 'AbortError') return
      attempt += 1
      if (attempt >= MAX_STREAM_ATTEMPTS) {
        turn.warning = ''
        pollUntilDone(turn, runId)
        return
      }
      await sleep(reconnectDelay(attempt))
    } finally {
      if (streamAbort === controller) streamAbort = null
    }
  }
}

async function send(text) {
  const value = (text ?? goal.value).trim()
  if (!value || running.value) return
  goal.value = ''
  const turn = newTurn(value)
  startClock()
  scrollToBottom()

  try {
    const started = await runApi.run({
      goal: value,
      context: '',
      max_rounds: 12,
      max_tool_calls: 24,
      max_seconds: 180,
    })
    turn.runId = started.run_id
    // The endpoint normally answers 202 with a run id; a synchronous answer
    // (a finished snapshot) must not be followed as if it were still running.
    turn.status = started.status || 'running'
    if (isTerminal(turn.status)) {
      finish(turn, started)
      return
    }
    follow(turn, started.run_id)
  } catch (err) {
    const detail = err?.response?.data?.detail
    fail(turn, detail || err.message || t('console.failed'))
  }
}

function reset() {
  stopTimers()
  turns.value = []
  goal.value = ''
}

async function loadMeta() {
  try {
    capabilities.value = await runApi.capabilities()
  } catch {
    capabilities.value = {}
  }
  try {
    const data = await runApi.list({ limit: 3 })
    recentRuns.value = data.runs || []
  } catch {
    recentRuns.value = []
  }
}

/** The topbar command bar routes here with ?goal=; start it once. */
function maybeRunFromQuery() {
  const value = route.query.goal
  if (typeof value === 'string' && value.trim()) {
    router.replace({ path: '/console' })
    send(value)
  }
}

onMounted(async () => {
  await loadMeta()
  maybeRunFromQuery()
})

watch(() => route.query.goal, maybeRunFromQuery)

// Leaving the page stops the stream. A run keeps going on the server; it is
// the connection that should not outlive the view.
onUnmounted(stopTimers)
</script>

<style scoped>
.console-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 52px - var(--space-6) - var(--space-8));
  max-width: 920px;
  margin: 0 auto;
}

.console-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-2) 0 var(--space-4);
}

/* ---------------------------------------------------------------- empty -- */
.console-empty {
  padding: var(--space-8) 0;
}

.agent-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  color: #fff;
  background: linear-gradient(135deg, var(--accent), var(--accent-hover));
  box-shadow: 0 6px 18px rgba(62, 99, 221, 0.25);
  font-size: 18px;
}
.agent-mark--sm {
  width: 24px;
  height: 24px;
  border-radius: 7px;
  font-size: 13px;
  box-shadow: none;
}

.console-empty h1 {
  margin: var(--space-4) 0 var(--space-2);
  font-size: var(--fs-metric-sm);
  font-weight: 600;
  color: var(--text-1);
  letter-spacing: -0.01em;
}

.console-intro {
  max-width: 560px;
  margin-bottom: var(--space-6);
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--text-3);
}

.suggestions-title {
  margin-bottom: var(--space-2);
  font-size: var(--fs-caption);
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--text-3);
  text-transform: uppercase;
}

.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-bottom: var(--space-6);
}
.suggestions button {
  border: 1px solid var(--border);
  background: var(--bg-card);
  color: var(--text-2);
  font-size: var(--fs-sm);
  padding: var(--space-2) var(--space-3);
  border-radius: 999px;
  cursor: pointer;
  transition: border-color 120ms ease, color 120ms ease;
}
.suggestions button:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.recent {
  border-top: 1px solid var(--border-subtle);
  padding-top: var(--space-4);
}
.recent-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-2) 0;
  color: var(--text-2);
  font-size: var(--fs-sm);
  border-bottom: 1px solid var(--border-subtle);
}
.recent-row:last-child {
  border-bottom: none;
}
.recent-goal {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* --------------------------------------------------------------- thread -- */
.thread {
  padding-bottom: var(--space-2);
}

.turn + .turn {
  margin-top: var(--space-6);
}

.msg-user {
  display: flex;
  justify-content: flex-end;
  margin-bottom: var(--space-4);
}
.msg-user p {
  max-width: 78%;
  padding: var(--space-3) var(--space-4);
  background: var(--accent);
  color: #fff;
  border-radius: 14px 14px 4px 14px;
  font-size: var(--fs-sm);
  line-height: 1.6;
}

.msg-agent {
  min-width: 0;
}

.agent-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}
.agent-meta {
  font-size: var(--fs-caption);
  color: var(--text-3);
}

.agent-warning {
  margin-bottom: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--negative);
}

.thinking-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.25; transform: scale(0.85); }
  50% { opacity: 1; transform: scale(1); }
}

.trace-block {
  margin-bottom: var(--space-3);
}
.trace-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  border: 1px solid var(--border-subtle);
  background: var(--bg-subtle);
  color: var(--text-2);
  font-size: var(--fs-caption);
  padding: var(--space-1) var(--space-3);
  border-radius: 999px;
  cursor: pointer;
}
.trace-hint {
  color: var(--text-3);
}

.conclusion {
  padding: var(--space-4);
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-sm);
  background: var(--bg-card);
}

.agent-foot {
  display: flex;
  gap: var(--space-4);
  margin-top: var(--space-2);
  font-size: var(--fs-caption);
}
.agent-foot a,
.agent-foot button {
  color: var(--text-3);
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  font-size: var(--fs-caption);
}
.agent-foot a:hover,
.agent-foot button:hover {
  color: var(--accent);
}

/* ------------------------------------------------------------- composer -- */
.composer {
  flex: none;
  padding-top: var(--space-3);
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-page);
}

.composer-box {
  display: flex;
  align-items: flex-end;
  gap: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--bg-card);
  transition: border-color 120ms ease;
}
.composer-box:focus-within {
  border-color: var(--accent);
}
.composer-box :deep(.el-textarea__inner) {
  border: none;
  box-shadow: none;
  padding: var(--space-2) var(--space-3);
  background: transparent;
  font-size: var(--fs-sm);
}

.composer-hint {
  margin-top: var(--space-1);
  font-size: 11px;
  color: var(--text-disabled);
  text-align: right;
}

/* Markdown body, kept in step with Runs.vue. */
.md {
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--text-1);
  word-break: break-word;
}
.md :deep(p) { margin: 0 0 var(--space-2); }
.md :deep(ul), .md :deep(ol) { padding-left: var(--space-5); margin: 0 0 var(--space-2); }
.md :deep(strong) { font-weight: 600; }
.md :deep(code) {
  background: var(--bg-subtle);
  padding: 0.1em 0.35em;
  border-radius: 4px;
  font-family: var(--font-mono);
  font-size: 0.88em;
}
.md :deep(table) { border-collapse: collapse; margin: var(--space-2) 0; font-size: 0.92em; }
.md :deep(th), .md :deep(td) {
  border: 1px solid var(--border-subtle);
  padding: var(--space-1) var(--space-2);
  text-align: left;
}
.md :deep(th) { background: var(--bg-subtle); }
</style>
