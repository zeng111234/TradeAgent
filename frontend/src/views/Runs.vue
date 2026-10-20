<template>
  <div>
    <div class="page-header">
      <h2>Agent 运行轨迹</h2>
      <p>
        给 agent 一个目标，它自己决定调用哪些工具。
        下面每一条都是真实执行过的路径 —— 不是手画的流程图。
      </p>
    </div>

    <!-- Not configured -->
    <el-alert
      v-if="!capabilities.configured"
      type="warning"
      :closable="false"
      show-icon
      title="未配置模型，agent 拒绝运行"
      style="margin-bottom: 16px"
    >
      <p style="margin: 4px 0 0">
        设置 <code>OPENAI_API_KEY</code> 后可用。
        没有降级方案 —— 一个无法判断的 agent 不是 agent。
        想看它如何工作：在 <code>backend/</code> 下运行
        <code>python -m app.cli demo</code>。
      </p>
    </el-alert>

    <!-- Run form first: it is the only action this page offers, and it used to
         sit below a wall of boundary documentation. -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>
        <span class="card-title">运行</span>
      </template>
      <el-form label-position="top">
        <el-form-item label="目标（用自然语言描述，不要描述步骤）">
          <el-input
            v-model="goal"
            type="textarea"
            :rows="2"
            placeholder="例如：看看我现有客户里哪些需要今天跟进，给我理由"
            :disabled="running"
          />
        </el-form-item>
        <el-form-item label="补充背景（可选）">
          <el-input
            v-model="context"
            type="textarea"
            :rows="2"
            placeholder="例如：我们下周参加法兰克福家纺展"
            :disabled="running"
          />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="6">
            <el-form-item label="最多轮次">
              <el-input-number v-model="maxRounds" :min="1" :max="30" :disabled="running" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="最多工具调用">
              <el-input-number v-model="maxToolCalls" :min="1" :max="60" :disabled="running" />
            </el-form-item>
          </el-col>
          <el-col :span="12" style="display: flex; align-items: flex-end; padding-bottom: 18px">
            <el-button type="primary" :loading="running" :disabled="!goal.trim() || !capabilities.configured"
              @click="startRun">
              <el-icon><VideoPlay /></el-icon> 运行 agent
            </el-button>
            <el-button :disabled="running" @click="loadRuns">刷新历史</el-button>
            <span v-if="running" class="hint" style="margin-left: 12px">
              后台运行，进度每秒刷新（上限 {{ maxSeconds }} 秒）
            </span>
          </el-col>
        </el-row>
      </el-form>
    </el-card>

    <!-- Live progress. The run is on the server; this card is what the last
         poll saw. Steps appear as rounds complete, because that is when the
         loop writes them -- not a character stream. -->
    <el-card v-if="active" shadow="never" style="margin-bottom: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600">正在运行</span>
          <div>
            <el-tag :type="statusType(active.status)" size="small" effect="plain">{{ active.status }}</el-tag>
            <span class="hint" style="margin-left: 8px">
              第 {{ active.rounds }} 轮 · {{ active.toolCalls }} 次工具调用 · {{ elapsedSeconds }}s
            </span>
          </div>
        </div>
      </template>

      <el-alert
        v-if="active.warning"
        type="warning"
        :closable="false"
        show-icon
        :title="active.warning"
        style="margin-bottom: 12px"
      />

      <p class="hint" style="margin: 0 0 8px">
        下面每一轮都是服务端已经落库的步骤，按完成顺序出现；不是前端推测，也不是逐字流。
      </p>

      <TraceTimeline v-if="active.steps.length" :steps="active.steps" />
      <p v-else class="hint" style="margin: 0">
        等待第一轮完成……（模型正在读目标、决定第一步）
      </p>

      <p v-if="active.runId" style="margin-top: 12px">
        <a :href="traceUrl(active.runId)" target="_blank" rel="noopener">
          在独立轨迹页查看这次运行（轮询卡住时的备用入口）
        </a>
      </p>
    </el-card>

    <!-- Result of the run just performed -->
    <el-card v-if="result" shadow="never" style="margin-bottom: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600">本次运行</span>
          <div>
            <el-tag :type="statusType(result.status)" size="small">{{ result.status }}</el-tag>
            <span class="hint" style="margin-left: 8px">{{ result.stop_reason }}</span>
          </div>
        </div>
      </template>
      <el-descriptions :column="2" border size="small" style="margin-bottom: 12px">
        <el-descriptions-item label="工具路径" :span="2">
          <code>{{ result.tool_sequence?.join(' → ') || '(未调用工具)' }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="轮次 / 调用">
          {{ result.rounds }} / {{ result.tool_calls }}
        </el-descriptions-item>
        <el-descriptions-item label="是否真的调查过">
          <el-tag :type="result.did_investigate ? 'success' : 'warning'" size="small">
            {{ result.did_investigate ? '是' : '否' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="耗时" :span="2">
          {{ result.elapsed }}s · {{ result.prompt_tokens }}+{{ result.completion_tokens }} tokens
        </el-descriptions-item>
      </el-descriptions>

      <div v-if="result.final_output" class="conclusion">
        <p class="section-label">结论</p>
        <div class="md" v-html="renderMarkdown(result.final_output)"></div>
      </div>
      <el-alert v-else-if="!result.did_investigate" type="warning" :closable="false" show-icon
        title="这次运行没有调用任何工具就给出了答复" />

      <p class="section-label" style="margin-top: 16px">执行路径</p>
      <TraceDiagram :source="result.mermaid_sequence" />
      <TraceTimeline :steps="result.steps" />
    </el-card>

    <!-- History -->
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600">历史运行（{{ runs.length }}）</span>
          <el-select v-model="statusFilter" size="small" clearable placeholder="全部状态"
            style="width: 160px" @change="loadRuns">
            <el-option label="succeeded" value="succeeded" />
            <el-option label="truncated" value="truncated" />
            <el-option label="stalled" value="stalled" />
            <el-option label="error" value="error" />
          </el-select>
        </div>
      </template>

      <el-table v-if="runs.length" :data="runs" size="small" @row-click="openRun" style="cursor: pointer">
        <el-table-column prop="run_id" label="run id" width="100">
          <template #default="{ row }"><code>{{ row.run_id.slice(0, 8) }}</code></template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="96">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <!-- The tool path is the signal this page exists for, so it gets room
             but not lines: the first two tools, a +N, and the whole sequence on
             hover. Truncating it to an ellipsis hid the point; moving it into
             the expansion row would fight the row click that opens the drawer. -->
        <el-table-column label="工具路径" width="220">
          <template #default="{ row }">
            <el-tooltip v-if="row.tool_sequence && row.tool_sequence.length" placement="top">
              <template #content>
                <span class="tool-path-full">{{ row.tool_sequence.join(' → ') }}</span>
              </template>
              <span class="tool-path-cell">
                <code>{{ row.tool_sequence.slice(0, 2).join(' → ') }}</code>
                <span v-if="row.tool_sequence.length > 2" class="tool-path-more">+{{ row.tool_sequence.length - 2 }}</span>
              </span>
            </el-tooltip>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="rounds" label="轮次" width="56" align="center" />
        <el-table-column prop="tool_calls" label="调用" width="56" align="center" />
        <el-table-column label="目标" min-width="230">
          <template #default="{ row }">
            <span class="goal-cell">{{ row.goal }}</span>
          </template>
        </el-table-column>
        <!-- An explicit action, because the diagram is the whole point of this
             page and a row click is not a visible affordance. -->
        <el-table-column label="操作" width="146" align="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click.stop="openRun(row)">
              查看轨迹
            </el-button>
            <el-button size="small" link @click.stop="openTracePage(row)">新窗口</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="还没有运行记录" />
    </el-card>

    <!-- Selected historical run -->
    <el-drawer v-model="drawerOpen" size="70%" :title="selected ? `运行 ${selected.run_id.slice(0, 8)}` : ''">
      <div v-if="selected">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="目标" :span="2">{{ selected.goal }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusType(selected.status)" size="small">{{ selected.status }}</el-tag>
            {{ selected.stop_reason }}
          </el-descriptions-item>
          <el-descriptions-item label="轮次 / 调用">
            {{ selected.rounds }} / {{ selected.tool_calls }}
          </el-descriptions-item>
        </el-descriptions>

        <div v-if="selected.final_output" class="conclusion" style="margin-top: 16px">
          <p class="section-label">结论</p>
          <div class="md" v-html="renderMarkdown(selected.final_output)"></div>
        </div>

        <p class="section-label" style="margin-top: 16px">执行路径</p>
        <TraceDiagram :source="selected.mermaid_sequence" />
        <TraceTimeline :steps="selected.steps" />

        <p style="margin-top: 16px">
          <a :href="traceUrl(selected.run_id)" target="_blank" rel="noopener">
            在新标签页打开独立轨迹页
          </a>
        </p>
      </div>
    </el-drawer>

    <!-- Boundary, demoted to a callout: it documents the permission model and
         is not an action, so it does not get a card of equal weight. The
         "deliberately absent" tools are a design note, not an error -- they
         are plain tags now, not danger. -->
    <div v-if="capabilities.tool_count" class="callout" style="margin-top: 16px">
      <p class="section-label">
        权限边界 —— agent 有 {{ capabilities.tool_count }} 个工具，但发送权不在它手上
      </p>
      <div class="boundary-grid">
        <div class="boundary-cell">
          <p class="callout-label">可写工具（仅此两个）</p>
          <div class="tag-cloud">
            <el-tag v-for="t in capabilities.write_capable" :key="t" size="small">
              {{ t }}
            </el-tag>
          </div>
          <p class="hint">
            两个都能写，但都写不到客户手上：一个只存 <code>pending</code> 草稿，一个遇到已存在的客户直接
            <code>skipped</code>。
          </p>
        </div>
        <div class="boundary-cell">
          <p class="callout-label">
            刻意不存在的工具
            <span class="badge">不是缺失，是设计</span>
          </p>
          <div class="tag-cloud">
            <el-tag v-for="c in capabilities.cannot" :key="c" size="small" effect="plain">
              {{ c }}
            </el-tag>
          </div>
          <p class="hint">
            这些不是我忘了加，是<strong>故意没有注册</strong>。边界靠"工具不存在"实现，不靠提示词要求模型自觉 ——
            一条提示词可以被绕过，一个不存在的工具不行。
          </p>
        </div>
      </div>
      <p class="hint boundary-foot">{{ capabilities.sending }}</p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { runApi } from '../api'
import { runStatusType as statusType } from '../constants/status.js'
import { renderMarkdown } from '../utils/markdown'
import { isTerminal, mergeSteps, normaliseRun } from '../utils/runProgress'
import { readEventStream, reconnectDelay } from '../utils/sse'
import TraceDiagram from '../components/TraceDiagram.vue'
import TraceTimeline from '../components/TraceTimeline.vue'

const capabilities = ref({ configured: false, tools: [], cannot: [], write_capable: [], sending: '' })
const goal = ref('')
const context = ref('')
const maxRounds = ref(12)
const maxToolCalls = ref(25)
const maxSeconds = ref(180)

const running = ref(false)
const result = ref(null)

// The run that is still going, and the shape the progress card renders. Kept
// apart from `result` on purpose: a half-finished trace is not a result, and
// the result panel assumes fields (tool_sequence, final_output) that only
// exist once the run is over.
const active = ref(null)
const clockTick = ref(0)

//: One second. A run is bounded at 180 s by default and each poll is a single
//: indexed row read, so this is cheap; faster would not show anything new,
//: because progress is written once per round, not continuously.
const POLL_INTERVAL_MS = 1000
//: Three consecutive failures before the UI stops trying. A dropped Wi-Fi
//: packet is not "the run failed", and saying so would be a lie about work
//: that is still happening on the server.
const MAX_POLL_FAILURES = 5
//: A run that outlives its own budget by a minute is not going to finish. The
//: server also sweeps abandoned runs at startup; this only stops the tab.
const POLL_GRACE_MS = 60_000

const elapsedSeconds = computed(() => {
  // Read the tick so this recomputes once a second even when nothing else on
  // the page changes. Local stopwatch, not the server's `elapsed`: the server
  // updates that only when a round completes, so between rounds it would sit
  // still and look frozen. Both numbers are honest; they measure different
  // things.
  void clockTick.value
  if (!active.value) return 0
  return Math.max(0, Math.round((Date.now() - active.value.startedAt) / 1000))
})

const runs = ref([])
const statusFilter = ref('')
const selected = ref(null)
const drawerOpen = ref(false)

function traceUrl(runId) {
  return runApi.traceUrl(runId)
}

async function loadCapabilities() {
  try {
    capabilities.value = await runApi.capabilities()
  } catch (err) {
    console.error('could not load agent capabilities', err)
  }
}

async function loadRuns() {
  try {
    const params = { limit: 50 }
    if (statusFilter.value) params.status = statusFilter.value
    const data = await runApi.list(params)
    runs.value = data.runs || []
  } catch (err) {
    console.error('could not load runs', err)
  }
}

let pollTimer = null
let clockTimer = null
let pollFailures = 0
let streamAbort = null

/** How many failed connection attempts before the page falls back to polling. */
const MAX_STREAM_ATTEMPTS = 3

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function abortStream() {
  if (streamAbort) {
    streamAbort.abort()
    streamAbort = null
  }
}

function startClock() {
  stopClock()
  clockTick.value = 0
  clockTimer = setInterval(() => {
    clockTick.value += 1
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

/**
 * One poll at a time, scheduled by the previous one finishing.
 *
 * ``setInterval`` would keep firing while a slow request is still open, and on
 * a hotel Wi-Fi that piles up requests the server has to answer after the user
 * has already left. A recursive ``setTimeout`` is the serial version.
 */
function schedulePoll(runId, delay = POLL_INTERVAL_MS) {
  stopPolling()
  pollTimer = setTimeout(() => {
    pollOnce(runId)
  }, delay)
}

async function pollOnce(runId) {
  // The user may have started another run, or left the page between ticks.
  if (!active.value || active.value.runId !== runId) return
  // A background tab is not watching. Browsers throttle timers there anyway;
  // this makes the intent explicit instead of relying on the throttle.
  if (typeof document !== 'undefined' && document.hidden) {
    schedulePoll(runId, POLL_INTERVAL_MS * 2)
    return
  }

  try {
    const snapshot = await runApi.get(runId)
    if (!active.value || active.value.runId !== runId) return
    pollFailures = 0
    applySnapshot(snapshot)

    if (isTerminal(snapshot.status)) {
      finish(snapshot)
      return
    }

    // The server bounds a run by `max_seconds`; a minute past that it is not
    // slow, it is gone. The tab stops asking rather than polling forever.
    if (Date.now() - active.value.startedAt > active.value.maxSeconds * 1000 + POLL_GRACE_MS) {
      stopWithWarning(
        '超过预算一分钟仍未结束，已停止刷新。服务端可能仍在运行，打开独立轨迹页可以看到最新状态。'
      )
      return
    }
    schedulePoll(runId)
  } catch (err) {
    if (err?.response?.status === 404) {
      stopWithWarning('这次运行已经不在服务端（可能被清理），列表里也没有它的最终结果。')
      return
    }
    pollFailures += 1
    if (pollFailures >= MAX_POLL_FAILURES) {
      stopWithWarning(
        `连续 ${MAX_POLL_FAILURES} 次读不到进度。服务端可能仍在运行，打开独立轨迹页确认。`
      )
      return
    }
    schedulePoll(runId, POLL_INTERVAL_MS * 3)
  }
}

function applySnapshot(snapshot) {
  const a = active.value
  if (!a) return
  a.status = snapshot.status || a.status
  a.rounds = snapshot.rounds ?? a.rounds
  a.toolCalls = snapshot.tool_calls ?? a.toolCalls
  a.stopReason = snapshot.stop_reason || a.stopReason
  a.steps = mergeSteps(a.steps, snapshot.steps || [])
}

function finish(snapshot) {
  stopPolling()
  stopClock()
  running.value = false
  const final = normaliseRun(snapshot)
  result.value = final
  active.value = null
  // "truncated" is a budget stop, not a failure; saying "运行结束" without the
  // distinction is how a partial answer gets read as a complete one.
  const label = final.status === 'truncated' ? '到达预算上限（不是失败）' : final.status
  ElMessage.success(`运行结束：${label}（${final.tool_calls} 次工具调用）`)
  loadRuns()
}

/**
 * Follow a run over SSE, falling back to polling if the stream cannot be held.
 *
 * The stream is preferred: one connection instead of one request per second,
 * and the server pushes when a round completes instead of the client asking
 * whether one did. The fallback exists because a stream depends on the whole
 * path -- browser, proxy, load balancer -- agreeing to keep one connection open.
 * A page that shows nothing when they do not is worse than one that asks again
 * every second.
 *
 * `lastId` is the resume cursor. It is the round number, not a sequence: the
 * terminal `done` carries the same id as the final `snapshot`, which is why a
 * reconnect at `after=<last round>` still receives the answer instead of waiting
 * for a round that will never come.
 */
async function watchRun(runId) {
  let attempt = 0
  let lastId = 0

  while (active.value && active.value.runId === runId) {
    const controller = new AbortController()
    streamAbort = controller

    try {
      const response = await fetch(runApi.eventsUrl(runId, lastId), {
        signal: controller.signal,
        headers: { Accept: 'text/event-stream' },
      })

      if (response.status === 404) {
        stopWithWarning('这次运行已经不在服务端（可能被清理）。')
        return
      }
      if (!response.ok) throw new Error(`stream answered ${response.status}`)

      attempt = 0
      const { droppedPartial } = await readEventStream(response, (frame) => {
        if (Number.isFinite(frame.id)) lastId = Math.max(lastId, frame.id)

        let payload
        try {
          payload = JSON.parse(frame.data)
        } catch {
          // A frame whose JSON does not parse is a protocol error, not a run
          // error. Drop it and let the reconnect re-request from `lastId`:
          // acting on half a payload is worse than acting on none.
          return
        }

        if (frame.event === 'error') {
          stopWithWarning(payload.message || '这次运行读不到。')
          return
        }
        if (frame.event === 'timeout') {
          stopWithWarning(payload.message || '连接打开太久，已断开；重新打开即可继续。')
          return
        }

        applySnapshot(payload)
        if (frame.event === 'done') finish(payload)
      })

      // The server closed without `done`: a restart, a proxy idle timeout, a
      // cut connection. Resume from where we are; do not restart the run.
      if (active.value && active.value.runId === runId) {
        if (droppedPartial) {
          console.warn('stream ended mid-frame; resuming from round', lastId)
        }
        await sleep(reconnectDelay(++attempt))
      }
    } catch (err) {
      if (err?.name === 'AbortError') return
      attempt += 1
      if (attempt >= MAX_STREAM_ATTEMPTS) {
        fallbackToPolling(runId)
        return
      }
      await sleep(reconnectDelay(attempt))
    } finally {
      if (streamAbort === controller) streamAbort = null
    }
  }
}

function fallbackToPolling(runId) {
  if (!active.value || active.value.runId !== runId) return
  active.value.warning = '实时连接不可用，已改为每秒刷新。'
  schedulePoll(runId)
}

function stopWithWarning(message) {
  abortStream()
  stopPolling()
  stopClock()
  running.value = false
  if (active.value) active.value.warning = message
  ElMessage.warning(message)
}

function showRunError(err) {
  const detail = err?.response?.data?.detail
  if (err?.response?.status === 503) {
    ElMessage.warning(detail || '未配置模型')
  } else if (err?.response?.status === 429) {
    ElMessage.warning(detail || '已有运行在跑，等它结束再试')
  } else {
    ElMessage.error(detail || err.message || '运行失败')
  }
}

async function startRun() {
  if (!goal.value.trim()) return
  result.value = null
  running.value = true
  active.value = {
    runId: null,
    goal: goal.value,
    startedAt: Date.now(),
    maxSeconds: maxSeconds.value,
    status: 'starting',
    rounds: 0,
    toolCalls: 0,
    stopReason: '',
    steps: [],
    warning: '',
  }
  startClock()

  try {
    const started = await runApi.run({
      goal: goal.value,
      context: context.value,
      max_rounds: maxRounds.value,
      max_tool_calls: maxToolCalls.value,
      max_seconds: maxSeconds.value,
    })
    // Defensive: if the endpoint ever answers synchronously, there is nothing
    // to poll. Today it answers 202 with the id and no steps.
    if (started.steps) {
      finish(started)
      return
    }
    if (!active.value) return
    active.value.runId = started.run_id
    active.value.status = started.status || 'running'
    if (started.max_seconds) active.value.maxSeconds = started.max_seconds
    watchRun(started.run_id)
  } catch (err) {
    active.value = null
    running.value = false
    stopClock()
    showRunError(err)
  }
}

async function openRun(row) {
  try {
    selected.value = await runApi.get(row.run_id)
    drawerOpen.value = true
  } catch (err) {
    ElMessage.error('无法读取该次运行的轨迹')
  }
}

/** The standalone page renders the same trace without the app shell. */
function openTracePage(row) {
  window.open(traceUrl(row.run_id), '_blank', 'noopener')
}

onMounted(() => {
  loadCapabilities()
  loadRuns()
})

// Leaving the page must stop the polling. A `setTimeout` chain holds a closure
// over the component and keeps calling the API for a tab nobody is looking at,
// and writing a ref after unmount is the ordinary way a Vue app leaks.
onUnmounted(() => {
  abortStream()
  stopPolling()
  stopClock()
  // Dropping the reference ends the `watchRun` loop even if it is currently
  // waiting out a backoff, where there is no timer this component owns.
  active.value = null
})
</script>

<style scoped>
.section-label {
  font-weight: 600;
  font-size: 13px;
  color: var(--text-2);
  margin: 0 0 8px;
}
.callout-label {
  font-weight: 600;
  font-size: var(--fs-caption);
  color: var(--text-2);
  margin: 0 0 var(--space-2);
}
/* A two-column el-row with :span="12" clipped the tag list on narrower windows,
   which is how the boundary came to look truncated. Flex wrap instead. */
.boundary-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
}
.boundary-cell {
  flex: 1 1 320px;
  min-width: 260px;
}
.tag-cloud {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
/* `cannot` entries are whole sentences ("promise a ship date: there is no
   schedule…"), not identifiers. An el-tag is nowrap by default, so one long
   sentence became a 550px pill and pushed the page 31px wide. Let the tag
   wrap and stay inside its cell. */
.tag-cloud :deep(.el-tag) {
  white-space: normal;
  height: auto;
  max-width: 100%;
  line-height: 1.5;
  padding: 2px 9px;
}
.badge {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
  color: var(--warning);
  background: var(--warning-weak);
  border: 1px solid var(--warning-border);
}
.boundary-foot {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border-subtle);
}
.hint {
  color: var(--text-3);
  font-size: 12px;
  margin: 6px 0 0;
}
.conclusion {
  border-left: 3px solid var(--accent);
  background: var(--bg-subtle);
  padding: 12px 16px;
  border-radius: 6px;
}
.conclusion-body {
  white-space: pre-wrap;
  font-size: 14px;
  line-height: 1.65;
  color: var(--text-1);
}
/* Rendered agent markdown. Kept in step with the standalone trace page. */
.md {
  font-size: 14px;
  line-height: 1.65;
  color: var(--text-1);
  word-break: break-word;
}
.md :deep(h2),
.md :deep(h3),
.md :deep(h4) {
  margin: 1rem 0 0.35rem;
  line-height: 1.3;
}
.md :deep(h2) { font-size: 1.02rem; }
.md :deep(h3) { font-size: 0.95rem; }
.md :deep(p) { margin: 0.45rem 0; }
.md :deep(ul),
.md :deep(ol) { margin: 0.35rem 0 0.55rem; padding-left: 1.25rem; }
.md :deep(li) { margin: 0.18rem 0; }
.md :deep(code) {
  background: var(--bg-subtle);
  padding: 0.1em 0.35em;
  border-radius: 4px;
  font-size: 0.88em;
}
.md :deep(pre.md-block) {
  background: var(--bg-subtle);
  border: 1px solid var(--border-subtle);
  padding: 0.7rem 0.9rem;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 0.85rem;
  white-space: pre-wrap;
}
.md :deep(table) { border-collapse: collapse; margin: 0.6rem 0; font-size: 0.9rem; }
.md :deep(th),
.md :deep(td) { border: 1px solid var(--border); padding: 0.3rem 0.6rem; text-align: left; }
.md :deep(th) { background: var(--bg-subtle); }
.md :deep(hr) { border: 0; border-top: 1px solid var(--border); margin: 0.9rem 0; }
.goal-cell {
  display: inline-block;
  max-width: 340px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* History row: two tools, then a count. Whole sequence on hover. */
.tool-path-cell {
  display: inline-flex;
  align-items: baseline;
  gap: var(--space-2);
  max-width: 100%;
  overflow: hidden;
  white-space: nowrap;
  vertical-align: bottom;
}
.tool-path-more {
  color: var(--text-3);
  font-size: var(--fs-caption);
  flex: none;
}
.tool-path-full {
  white-space: pre-wrap;
  font-family: var(--font-mono);
  font-size: var(--fs-caption);
}
code {
  font-size: 12px;
}
</style>
