<template>
  <div class="console-shell">
    <!-- Thread rail. Persistent above 1280px; a slide-over below that, because
         a 920px reading column plus a 248px rail does not fit a laptop. -->
    <aside class="thread-rail" :class="{ 'is-open': railOpen }">
      <div class="rail-head">
        <span class="rail-title">{{ t('console.historyTitle') }}</span>
      </div>
      <button type="button" class="rail-new" @click="startNewConversation">
        <el-icon><Plus /></el-icon>
        {{ t('console.newChat') }}
      </button>

      <div class="rail-list">
        <p v-if="!conversations.length" class="rail-empty">
          {{ t('console.conversationEmpty') }}
        </p>
        <button
          v-for="item in conversations"
          :key="item.conversation_id"
          type="button"
          class="rail-item"
          :class="{ active: item.conversation_id === conversationId }"
          @click="openConversation(item.conversation_id)"
        >
          <span class="rail-item__title">{{ item.title }}</span>
          <span class="rail-item__meta">
            {{ t('console.turnCount', { n: item.run_count }) }}
            <template v-if="item.last_status"> · {{ runLabel(item.last_status) }}</template>
          </span>
        </button>
      </div>
    </aside>

    <div v-if="railOpen" class="rail-backdrop" @click="railOpen = false"></div>

    <div class="console-page">
      <div class="console-head">
        <button type="button" class="rail-toggle" @click="railOpen = true">
          <el-icon><Menu /></el-icon>
          <span>{{ t('console.historyTitle') }}</span>
        </button>
        <span class="console-head__title">{{ currentTitle }}</span>
        <button type="button" class="head-new" @click="startNewConversation">
          {{ t('console.newChat') }}
        </button>
      </div>

      <div ref="scroller" class="console-scroll" @scroll.passive="onScroll">
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

        <div v-if="conversations.length" class="recent">
          <p class="suggestions-title">{{ t('console.historyTitle') }}</p>
          <button
            v-for="item in conversations.slice(0, 3)"
            :key="item.conversation_id"
            type="button"
            class="recent-row"
            @click="openConversation(item.conversation_id)"
          >
            <span class="recent-goal">{{ item.title }}</span>
            <el-tag
              v-if="item.last_status"
              :type="statusType(item.last_status)"
              size="small"
              effect="plain"
            >{{ runLabel(item.last_status) }}</el-tag>
          </button>
          <router-link to="/runs" class="recent-all">{{ t('console.allRuns') }}</router-link>
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
                <template v-if="stepTokens(turn)">
                  · {{ t('console.tokens', { n: formatTokens(stepTokens(turn)) }) }}
                </template>
              </span>
            </div>

            <p v-if="turn.warning" class="agent-warning">{{ turn.warning }}</p>

            <!-- Live: steps appear as rounds land, then collapse into a toggle. -->
            <div v-if="isRunning(turn)" class="thinking">
              <p class="thinking-line">
                <span class="pulse"></span>
                {{ liveStatus(turn) }}
              </p>
              <TraceTimeline
                v-if="turn.steps.length || turn.live"
                :steps="turn.steps"
                :live="turn.live"
              />
            </div>

            <template v-else>
              <div v-if="turn.steps.length" class="trace-block">
                <button type="button" class="trace-toggle" @click="turn.showTrace = !turn.showTrace">
                  <el-icon><ArrowRight /></el-icon>
                  {{ t('console.traceToggle', { n: turn.calls }) }}
                  <span class="trace-hint">{{ turn.showTrace ? '−' : '+' }}</span>
                </button>
                <TraceTimeline v-if="turn.showTrace" :steps="turn.steps" />
              </div>

              <div v-if="turn.result?.final_output" class="conclusion">
                <div class="md" v-html="renderMarkdown(turn.result.final_output)"></div>
              </div>
              <p v-if="outcome(turn) === 'budget'" class="agent-note">
                {{ t('console.budgetStop') }}
              </p>
              <p v-if="!turn.result?.final_output && turn.error" class="agent-warning">
                {{ turn.error }}
              </p>
              <p
                v-if="!turn.result?.final_output && !turn.error && turn.status === 'cancelled'"
                class="agent-note"
              >{{ t('console.cancelledNoAnswer') }}</p>

              <div v-if="turn.result" class="result-meta">
                <span v-if="turn.result.model" class="meta-chip meta-chip--model">
                  {{ turn.result.model }}
                </span>
                <span
                  v-if="tokenTotal(turn)"
                  class="meta-chip"
                  :title="t('console.tokensTip', { n: formatTokens(tokenTotal(turn)) })"
                >{{ t('console.tokensIn', { n: formatTokens(turn.result.prompt_tokens) }) }}</span>
                <span v-if="tokenTotal(turn)" class="meta-chip">
                  {{ t('console.tokensOut', { n: formatTokens(turn.result.completion_tokens) }) }}
                </span>
                <span v-if="cacheLabel(turn)" class="meta-chip" :title="cacheTip(turn)">
                  {{ cacheLabel(turn) }}
                </span>
                <span
                  v-if="costLabel(turn)"
                  class="meta-chip meta-chip--cost"
                  :title="costTip(turn)"
                >{{ costLabel(turn) }}</span>
              </div>

              <div v-if="turn.result?.final_output" class="result-actions">
                <button type="button" @click="copyResult(turn)">
                  {{ copiedTurn === turn.id ? t('console.copied') : t('console.copy') }}
                </button>
                <button type="button" :disabled="running" @click="rerun(turn)">
                  {{ t('console.rerun') }}
                </button>
                <button type="button" @click="exportResult(turn)">
                  {{ t('console.export') }}
                </button>
              </div>

              <div class="agent-foot">
                <a v-if="turn.runId" :href="traceUrl(turn.runId)" target="_blank" rel="noopener">
                  {{ t('console.openTrace') }}
                </a>
                <router-link
                  v-if="turn.runId"
                  :to="{ path: '/runs', query: { run: turn.runId } }"
                >{{ t('console.openRun') }}</router-link>
                <button type="button" @click="startNewConversation">{{ t('console.newChat') }}</button>
              </div>
            </template>
          </div>
        </article>
      </div>
    </div>

    <!-- Composer -->
    <button
      v-if="!followTail"
      type="button"
      class="jump-latest"
      @click="scrollToBottom(true)"
    >{{ t('console.jumpLatest') }}</button>
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
          v-if="running"
          type="danger"
          plain
          :disabled="activeTurn?.status === 'cancelling'"
          @click="stopActive()"
        >{{ activeTurn?.status === 'cancelling' ? t('console.stopping') : t('console.stop') }}</el-button>
        <el-button
          v-else
          type="primary"
          :disabled="!goal.trim() || capabilities.configured === false"
          @click="send()"
        >{{ t('console.send') }}</el-button>
      </div>
      <p class="composer-hint">Enter ↵ · Shift+Enter</p>
    </div>
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
import { formatCost, formatTokens, isTerminal, mergeSteps, normaliseRun, runOutcome } from '../utils/runProgress'
import { applyDelta, applyLive, resetLive } from '../utils/liveStream'
import { readEventStream, reconnectDelay } from '../utils/sse'
import TraceTimeline from '../components/TraceTimeline.vue'
import { t } from '../i18n/index.js'

const MAX_STREAM_ATTEMPTS = 3
const POLL_INTERVAL_MS = 1200
/** Which thread to reopen after a refresh. Mirrors the query parameter. */
const CONVERSATION_KEY = 'tradeagent.conversation'

const route = useRoute()
const router = useRouter()

const capabilities = ref({})
const turns = ref([])
const goal = ref('')
const conversations = ref([])
const conversationId = ref('')
const railOpen = ref(false)
const scroller = ref(null)
const followTail = ref(true)

let streamAbort = null
let pollTimer = null
let clockTimer = null
let turnSeq = 0

const running = computed(() => turns.value.some((turn) => isRunning(turn)))
/** The turn the composer's stop button applies to. One run at a time. */
const activeTurn = computed(() => turns.value.find(isRunning) || null)
const currentTitle = computed(() => {
  const found = conversations.value.find(
    (item) => item.conversation_id === conversationId.value
  )
  return found?.title || t('console.title')
})

const suggestions = computed(() => [
  t('console.suggestion1'),
  t('console.suggestion2'),
  t('console.suggestion3'),
])

// `cancelling` is still running: the stop is a request, and the stream keeps
// delivering the rounds that were already in flight until the terminal frame.
const isRunning = (turn) =>
  turn.status === 'starting' || turn.status === 'running' || turn.status === 'cancelling'
const runLabel = (status) => t(`status.run.${status}`)
const traceUrl = (runId) => runApi.traceUrl(runId)
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** The one line above the timeline, which now says what is actually happening. */
function liveStatus(turn) {
  if (turn.live?.content) return t('console.writing')
  if (turn.live?.reasoning) return t('console.thinkingLive')
  return turn.steps.length ? t('console.running') : t('console.waitingFirstRound')
}

const outcome = (turn) =>
  runOutcome(turn.status, turn.result?.stop_code, turn.result?.refused_calls)

const tokenTotal = (turn) =>
  (turn.result?.prompt_tokens || 0) + (turn.result?.completion_tokens || 0)

/** Tokens spent so far, summed over the rounds that have landed. */
const stepTokens = (turn) =>
  (turn.steps || []).reduce(
    (total, step) => total + (step.prompt_tokens || 0) + (step.completion_tokens || 0),
    0,
  )

/** Cost, or the honest reason there is none. Never `$0.00` for "unknown". */
function costLabel(turn) {
  const result = turn.result
  if (!result) return ''
  if (result.usage_complete === false) return t('console.usageMissing')
  if (!result.cost) return t('console.unpriced')
  return t('console.costLabel', { v: formatCost(result.cost) })
}

function cacheLabel(turn) {
  const rate = turn.result?.cache_hit_rate
  if (rate === null || rate === undefined) return ''
  return t('console.cacheRate', { n: Math.round(rate * 100) })
}

/** Where the cache hit came from, so the percentage is checkable. */
function cacheTip(turn) {
  const result = turn.result || {}
  return t('console.cacheTip', {
    hit: formatTokens(result.cached_prompt_tokens || 0),
    total: formatTokens(result.prompt_tokens || 0),
  })
}

/** The split the amount was computed from, and why it is only an estimate. */
function costTip(turn) {
  const result = turn.result || {}
  if (result.usage_complete === false) return t('console.usageMissingTip')
  if (!result.cost) return t('console.unpricedTip')
  const cached = result.cached_prompt_tokens || 0
  const parts = [
    t('console.costTip', {
      fresh: formatTokens(Math.max(0, (result.prompt_tokens || 0) - cached)),
      cached: formatTokens(cached),
      out: formatTokens(result.completion_tokens || 0),
    }),
    result.cost.peak ? t('console.costPeak') : t('console.costOffPeak'),
  ]
  if (result.cost.source) parts.push(result.cost.source)
  if (result.cost.estimated) parts.push(t('console.costEstimated'))
  return parts.join(' · ')
}

const copiedTurn = ref(0)

async function copyResult(turn) {
  const text = turn.result?.final_output || ''
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // Clipboard API needs a secure context; the fallback works on plain HTTP,
    // which is how this app is usually reached on a LAN.
    const area = document.createElement('textarea')
    area.value = text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    document.body.removeChild(area)
  }
  copiedTurn.value = turn.id
  setTimeout(() => {
    if (copiedTurn.value === turn.id) copiedTurn.value = 0
  }, 1600)
}

function rerun(turn) {
  if (running.value) return
  send(turn.goal)
}

/** Export without a backend endpoint: the answer is already on the client. */
function exportResult(turn) {
  const text = turn.result?.final_output || ''
  if (!text) return
  const header = `# ${turn.goal}\n\n`
  const blob = new Blob([header + text], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `agent-${turn.runId?.slice(0, 8) || 'result'}.md`
  link.click()
  URL.revokeObjectURL(url)
}

const elapsed = (turn) => Math.max(0, Math.round(((turn.endedAt || Date.now()) - turn.startedAt) / 1000))

/**
 * Follow the tail only while the reader is already at the bottom.
 *
 * Streaming made the old unconditional jump a real defect: at ten deltas a
 * second, scrolling up to read an earlier tool call snapped back before the
 * sentence could be read. `followTail` is released the moment the reader moves
 * away, and only an explicit jump or a new message re-arms it.
 */
function onScroll() {
  const el = scroller.value
  if (!el) return
  followTail.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

function scrollToBottom(force = false) {
  nextTick(() => {
    const el = scroller.value
    if (el && (force || followTail.value)) el.scrollTop = el.scrollHeight
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
    live: null,
    cancelRequested: false,
    warning: '',
    error: '',
    result: null,
    finished: false,
    showTrace: false,
  })
  turns.value.push(turn)
  return turn
}

function absorbSnapshot(turn, snapshot) {
  const next = snapshot.status || turn.status
  // A stop is a request. Until the row reaches a terminal status it still says
  // "running", and letting that overwrite the local `cancelling` would flip the
  // button back and re-enable a stop the user already asked for.
  turn.status = turn.cancelRequested && !isTerminal(next) ? 'cancelling' : next
  turn.rounds = snapshot.rounds ?? turn.rounds
  turn.calls = snapshot.tool_calls ?? turn.calls
  turn.steps = mergeSteps(turn.steps, snapshot.steps || [])
  // A persisted round supersedes the live buffer for that round. Not on every
  // snapshot: the first snapshot of a run legitimately reports `rounds: 0`
  // while the first round is already streaming, and clearing there would erase
  // text that has no replay path until the round ends.
  if (turn.live && (isTerminal(turn.status) || (snapshot.rounds ?? 0) >= turn.live.round)) {
    resetLive(turn)
  }
}

function applySnapshot(turn, snapshot) {
  absorbSnapshot(turn, snapshot)
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
  resetLive(turn)
  turn.result = normaliseRun(snapshot)
  turn.cancelRequested = false
  if (turn.status === 'error') turn.error = turn.result.stop_reason || t('console.failed')
  // The rail shows each thread's last status; the row just changed, so refresh
  // it once per finished turn rather than leaving "running" next to a thread
  // that has been done for minutes.
  loadConversations()
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
        // `live`/`delta` carry no id, so the resume cursor above is untouched:
        // a partial round must not move it. The snapshot remains the authority
        // for everything that is persisted.
        if (frame.event === 'live') {
          applyLive(turn, payload)
          scrollToBottom()
          return
        }
        if (frame.event === 'delta') {
          applyDelta(turn, payload)
          scrollToBottom()
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

/**
 * Ask the server to stop the active run.
 *
 * Optimistic: the button flips to "stopping" immediately, and the stream is
 * what settles the final status. A 409 means the run already finished -- not an
 * error to report, because the terminal frame is on its way with the answer.
 */
async function stopActive() {
  const turn = activeTurn.value
  if (!turn || turn.status === 'cancelling') return
  turn.cancelRequested = true

  // No run id yet: `send()` issues the stop the moment the POST returns.
  if (!turn.runId) return

  turn.status = 'cancelling'
  try {
    await runApi.cancel(turn.runId)
  } catch (err) {
    if (err?.response?.status === 409) return
    turn.cancelRequested = false
    turn.status = 'running'
    turn.warning = err?.response?.data?.detail || t('console.failed')
  }
}

async function send(text) {
  const value = (text ?? goal.value).trim()
  if (!value || running.value) return
  goal.value = ''
  const turn = newTurn(value)
  startClock()
  scrollToBottom(true)

  try {
    const started = await runApi.run({
      goal: value,
      conversation_id: conversationId.value || undefined,
      context: '',
      max_rounds: 12,
      max_tool_calls: 24,
      max_seconds: 180,
    })
    turn.runId = started.run_id
    // The server opens a thread when none was named. Adopt its id so the next
    // question continues it and the rail can highlight it.
    if (started.conversation_id) {
      conversationId.value = started.conversation_id
      try {
        localStorage.setItem(CONVERSATION_KEY, conversationId.value)
      } catch {
        // Ignored: the id is still in memory for this session.
      }
      if (route.query.conversation !== conversationId.value) {
        router.replace({ path: '/console', query: { conversation: conversationId.value } })
      }
      loadConversations()
    }
    // The endpoint normally answers 202 with a run id; a synchronous answer
    // (a finished snapshot) must not be followed as if it were still running.
    turn.status = started.status || 'running'
    if (isTerminal(turn.status)) {
      finish(turn, started)
      return
    }
    if (turn.cancelRequested) {
      // Stop was clicked while the POST was in flight. There was no id to
      // cancel then; now there is.
      turn.status = 'cancelling'
      try {
        await runApi.cancel(turn.runId)
      } catch {
        turn.cancelRequested = false
        turn.status = 'running'
      }
    }
    follow(turn, started.run_id)
  } catch (err) {
    const detail = err?.response?.data?.detail
    fail(turn, detail || err.message || t('console.failed'))
  }
}

function startNewConversation() {
  stopTimers()
  turns.value = []
  goal.value = ''
  conversationId.value = ''
  followTail.value = true
  railOpen.value = false
  try {
    localStorage.removeItem(CONVERSATION_KEY)
  } catch {
    // Private mode: the thread still works, it just will not survive a reload.
  }
  if (route.query.conversation) router.replace({ path: '/console' })
  scrollToBottom(true)
}

async function loadConversations() {
  try {
    const data = await runApi.conversations({ limit: 30 })
    conversations.value = data.conversations || []
  } catch {
    conversations.value = []
  }
}

/**
 * Rebuild a thread from the server.
 *
 * A turn still `running` keeps its stream: a refresh must not turn a live run
 * into a dead card whose answer never arrives.
 */
function hydrate(detail) {
  const restored = (detail.turns || []).map((snapshot) => {
    const created = snapshot.created_at ? Date.parse(snapshot.created_at) : NaN
    const ended = snapshot.finished_at ? Date.parse(snapshot.finished_at) : NaN
    const turn = reactive({
      id: ++turnSeq,
      goal: snapshot.goal,
      runId: snapshot.run_id,
      status: snapshot.status,
      startedAt: Number.isFinite(created) ? created : Date.now(),
      endedAt: Number.isFinite(ended) ? ended : 0,
      rounds: snapshot.rounds || 0,
      calls: snapshot.tool_calls || 0,
      steps: snapshot.steps || [],
      live: null,
      cancelRequested: false,
      warning: '',
      error: '',
      result: normaliseRun(snapshot),
      finished: !isRunning({ status: snapshot.status }),
      showTrace: false,
    })
    if (snapshot.status === 'error') turn.error = snapshot.stop_reason || ''
    return turn
  })

  turns.value = restored
  const live = restored.find((turn) => isRunning(turn))
  if (live?.runId) follow(live, live.runId)
}

async function openConversation(id, { silent = false } = {}) {
  if (!id) return
  if (id === conversationId.value && turns.value.length) {
    railOpen.value = false
    return
  }

  stopTimers()
  turns.value = []
  conversationId.value = id
  railOpen.value = false
  try {
    localStorage.setItem(CONVERSATION_KEY, id)
  } catch {
    // Ignored: see startNewConversation.
  }
  if (!silent) router.replace({ path: '/console', query: { conversation: id } })

  try {
    const detail = await runApi.conversation(id)
    hydrate(detail)
    scrollToBottom(true)
  } catch {
    // A thread deleted on the server (or a stale link). Forget it rather than
    // showing an error the reader cannot act on.
    conversationId.value = ''
    try {
      localStorage.removeItem(CONVERSATION_KEY)
    } catch {
      // Ignored.
    }
    if (!silent) router.replace({ path: '/console' })
  }
}

async function restoreConversation() {
  const query = typeof route.query.conversation === 'string' ? route.query.conversation : ''
  let stored = ''
  try {
    stored = localStorage.getItem(CONVERSATION_KEY) || ''
  } catch {
    stored = ''
  }
  const id = query || stored
  if (id) await openConversation(id, { silent: true })
}

async function loadMeta() {
  try {
    capabilities.value = await runApi.capabilities()
  } catch {
    capabilities.value = {}
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
  await loadConversations()
  // Restore before a topbar command: a goal typed into the shell should join
  // the thread the reader was in, not silently open a new one.
  await restoreConversation()
  maybeRunFromQuery()
})

watch(() => route.query.goal, maybeRunFromQuery)

// Leaving the page stops the stream. A run keeps going on the server; it is
// the connection that should not outlive the view.
onUnmounted(stopTimers)
</script>

<style scoped>
.console-shell {
  display: grid;
  grid-template-columns: 248px minmax(0, 920px);
  /* The row must be bounded by the shell, not by its content. With the default
     `auto` row, a long transcript sized the page -- the page then sized the
     row -- and the whole document grew past the viewport: the body scrolled,
     the sticky sidebar slid up out of view and left a bare strip under itself,
     and the composer ended up below the fold. `minmax(0, 1fr)` hands the
     scrolling back to `.console-scroll`, which is what has `overflow-y`; the
     page itself gets `min-height: 0` for the same reason. Either alone is
     enough today, and both are stated so neither has to be rediscovered. */
  grid-template-rows: minmax(0, 1fr);
  gap: var(--space-5, 24px);
  /* Start, not center. The tracks are 1192px wide inside a 1316px column, and
     centring put 96px of nothing between the dark sidebar and the thread rail.
     Starting at the same 32px gutter as the topbar breadcrumb lines the rail
     up with it and puts the unused width on the right, where it reads as a
     margin instead of a hole. */
  justify-content: start;
  height: calc(100vh - 52px - var(--space-6) - var(--space-8));
}

.console-page {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-width: 0;
  min-height: 0;
}

/* ------------------------------------------------------------ thread rail -- */
.thread-rail {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding-top: var(--space-2);
  border-right: 1px solid var(--border-subtle);
}
.rail-head {
  padding: 0 10px 8px;
}
.rail-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-3);
}
.rail-new {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 8px 10px;
  padding: 8px 10px;
  width: calc(100% - 16px);
  border: 1px dashed var(--border);
  border-radius: 8px;
  background: transparent;
  color: var(--text-2);
  font-size: 13px;
  cursor: pointer;
}
.rail-new:hover {
  color: var(--accent);
  border-color: var(--accent);
}
.rail-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 0 8px 16px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.rail-empty {
  padding: 8px 10px;
  font-size: 12px;
  color: var(--text-3);
}
.rail-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  padding: 9px 10px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.rail-item:hover {
  background: var(--bg-subtle);
}
.rail-item.active {
  background: var(--accent-weak, var(--bg-subtle));
}
.rail-item__title {
  font-size: 13px;
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rail-item__meta {
  font-size: 11px;
  color: var(--text-3);
}
.rail-backdrop {
  display: none;
}

/* ----------------------------------------------------------- page header -- */
.console-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 34px;
  padding-bottom: var(--space-2);
}
.console-head__title {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 13px;
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rail-toggle,
.head-new {
  display: none;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-2);
  font-size: 12px;
  cursor: pointer;
}
.rail-toggle:hover,
.head-new:hover {
  color: var(--accent);
  background: var(--bg-subtle);
}

/* Below this width the reading column stops being the constraint: the rail
   would squeeze it, so it slides over instead. */
@media (max-width: 1279px) {
  .console-shell {
    grid-template-columns: minmax(0, 1fr);
  }
  .thread-rail {
    position: fixed;
    top: 52px;
    bottom: 0;
    /* Start where the sidebar ends. At left: 0 the drawer slid out underneath
       it -- the app's sidebar is sticky with a higher z-index -- so 220 of its
       272 pixels were hidden and only a sliver of clipped text showed. */
    left: var(--sidebar-width);
    width: 272px;
    z-index: 40;
    padding: var(--space-3);
    background: var(--bg-card);
    border-right: 1px solid var(--border);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.12);
    transform: translateX(-100%);
    transition: transform 0.18s ease;
  }
  .thread-rail.is-open {
    transform: none;
  }
  .rail-backdrop {
    display: block;
    position: fixed;
    /* The sidebar stays usable: it is global navigation, not part of the
       drawer, and dimming it made the whole left edge look like a black bar. */
    inset: 52px 0 0 var(--sidebar-width);
    z-index: 39;
    background: rgba(0, 0, 0, 0.18);
  }
  .rail-toggle,
  .head-new {
    display: inline-flex;
  }
}

.console-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-2) 0 var(--space-4);
}

/* Shown only when streaming stopped following the tail, so the reader is not
   yanked back mid-sentence but can still return with one click. */
.jump-latest {
  position: absolute;
  left: 50%;
  bottom: 96px;
  transform: translateX(-50%);
  z-index: 2;
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg-card);
  color: var(--text-2);
  font-size: 12px;
  cursor: pointer;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.08);
}
.jump-latest:hover {
  color: var(--accent);
  border-color: var(--accent);
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
  width: 100%;
  padding: var(--space-2) 0;
  border: 0;
  border-bottom: 1px solid var(--border-subtle);
  background: transparent;
  text-align: left;
  font-family: inherit;
  cursor: pointer;
  color: var(--text-2);
  font-size: var(--fs-sm);
}
.recent-row:hover {
  color: var(--accent);
}
.recent-all {
  display: inline-block;
  margin-top: var(--space-2);
  font-size: 12px;
  color: var(--accent);
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

/* A stop is a fact, not a failure: same weight as a warning, neutral colour. */
.agent-note {
  margin-bottom: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--text-2);
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

/* What the run cost, in the only place a reader looks for it: under the answer.
   Numbers only; a missing price says "unpriced", not zero. */
.result-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-3);
  font-size: 11px;
  color: var(--text-3);
  font-variant-numeric: tabular-nums;
}
/* Usage as separate pills: "8.3k tokens / unpriced / 87%" in one grey line
   made the three numbers look like one sentence and the amount unreadable. */
.meta-chip {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: var(--bg-subtle);
  white-space: nowrap;
  cursor: default;
}
.meta-chip--model {
  color: var(--text-2);
}
.meta-chip--cost {
  color: var(--text-1);
  border-color: var(--border);
  background: var(--bg-card);
}
.result-actions {
  display: flex;
  gap: var(--space-3);
  margin-top: var(--space-2);
}
.result-actions button {
  padding: 3px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: transparent;
  color: var(--text-2);
  font-size: 12px;
  cursor: pointer;
}
.result-actions button:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent);
}
.result-actions button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
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
