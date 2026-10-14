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

    <!-- Boundary -->
    <el-card v-if="capabilities.tool_count" shadow="never" style="margin-bottom: 16px">
      <template #header>
        <span style="font-weight: 600">
          权限边界 —— agent 有 {{ capabilities.tool_count }} 个工具，但发送权不在它手上
        </span>
      </template>
      <el-row :gutter="20">
        <el-col :span="12">
          <p class="section-label">可写工具（仅此两个）</p>
          <el-tag v-for="t in capabilities.write_capable" :key="t" size="small" style="margin: 0 6px 6px 0">
            {{ t }}
          </el-tag>
        </el-col>
        <el-col :span="12">
          <p class="section-label">不存在的工具</p>
          <el-tag v-for="c in capabilities.cannot" :key="c" type="danger" size="small" effect="plain"
            style="margin: 0 6px 6px 0">
            {{ c }}
          </el-tag>
          <p class="hint">{{ capabilities.sending }}</p>
        </el-col>
      </el-row>
    </el-card>

    <!-- Run form -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>
        <span style="font-weight: 600">运行</span>
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
              同步执行，最多等待 {{ maxSeconds }} 秒
            </span>
          </el-col>
        </el-row>
      </el-form>
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
      <el-descriptions :column="4" border size="small" style="margin-bottom: 12px">
        <el-descriptions-item label="工具路径">
          <code>{{ result.tool_sequence.join(' → ') || '(未调用工具)' }}</code>
        </el-descriptions-item>
        <el-descriptions-item label="轮次 / 调用">
          {{ result.rounds }} / {{ result.tool_calls }}
        </el-descriptions-item>
        <el-descriptions-item label="是否真的调查过">
          <el-tag :type="result.did_investigate ? 'success' : 'warning'" size="small">
            {{ result.did_investigate ? '是' : '否' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="耗时">
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
        <el-table-column prop="run_id" label="run id" width="120">
          <template #default="{ row }"><code>{{ row.run_id.slice(0, 8) }}</code></template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="工具路径" min-width="320">
          <template #default="{ row }">
            <code style="font-size: 12px">{{ row.tool_sequence?.join(' → ') || '—' }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="rounds" label="轮次" width="70" />
        <el-table-column prop="tool_calls" label="调用" width="70" />
        <el-table-column label="目标" min-width="240">
          <template #default="{ row }">
            <span class="goal-cell">{{ row.goal }}</span>
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
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { runApi } from '../api'
import { renderMarkdown } from '../utils/markdown'
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

const runs = ref([])
const statusFilter = ref('')
const selected = ref(null)
const drawerOpen = ref(false)

function statusType(status) {
  return {
    succeeded: 'success',
    truncated: 'warning',
    stalled: 'warning',
    error: 'danger',
  }[status] || 'info'
}

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

async function startRun() {
  if (!goal.value.trim()) return
  running.value = true
  result.value = null
  try {
    result.value = await runApi.run({
      goal: goal.value,
      context: context.value,
      max_rounds: maxRounds.value,
      max_tool_calls: maxToolCalls.value,
      max_seconds: maxSeconds.value,
    })
    ElMessage.success(`运行完成：${result.value.status}（${result.value.tool_calls} 次工具调用）`)
    await loadRuns()
  } catch (err) {
    const detail = err?.response?.data?.detail
    if (err?.response?.status === 503) {
      ElMessage.warning(detail || '未配置模型')
    } else {
      ElMessage.error(detail || err.message || '运行失败')
    }
  } finally {
    running.value = false
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

onMounted(() => {
  loadCapabilities()
  loadRuns()
})
</script>

<style scoped>
.section-label {
  font-weight: 600;
  font-size: 13px;
  color: #606266;
  margin: 0 0 8px;
}
.hint {
  color: #909399;
  font-size: 12px;
  margin: 6px 0 0;
}
.conclusion {
  border-left: 3px solid #409eff;
  background: #f5f7fa;
  padding: 12px 16px;
  border-radius: 6px;
}
.conclusion-body {
  white-space: pre-wrap;
  font-size: 14px;
  line-height: 1.65;
  color: #303133;
}
/* Rendered agent markdown. Kept in step with the standalone trace page. */
.md {
  font-size: 14px;
  line-height: 1.65;
  color: #303133;
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
  background: #eef1f6;
  padding: 0.1em 0.35em;
  border-radius: 4px;
  font-size: 0.88em;
}
.md :deep(pre.md-block) {
  background: #f5f7fa;
  border: 1px solid #ebeef5;
  padding: 0.7rem 0.9rem;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 0.85rem;
  white-space: pre-wrap;
}
.md :deep(table) { border-collapse: collapse; margin: 0.6rem 0; font-size: 0.9rem; }
.md :deep(th),
.md :deep(td) { border: 1px solid #dcdfe6; padding: 0.3rem 0.6rem; text-align: left; }
.md :deep(th) { background: #f5f7fa; }
.md :deep(hr) { border: 0; border-top: 1px solid #dcdfe6; margin: 0.9rem 0; }
.goal-cell {
  display: inline-block;
  max-width: 340px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
code {
  font-size: 12px;
}
</style>
