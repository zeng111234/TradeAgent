<template>
  <div>
    <div class="page-header">
      <h2>{{ t('quality.title') }}</h2>
      <p>
        {{ t('quality.subtitlePart1') }} <code>—</code> {{ t('quality.subtitlePart2') }} <code>0%</code> {{ t('quality.subtitlePart3') }}
      </p>
    </div>

    <div class="toolbar">
      <el-radio-group v-model="days" :disabled="loading" @change="load">
        <el-radio-button :label="7">{{ t('quality.daysShort', { n: 7 }) }}</el-radio-button>
        <el-radio-button :label="14">{{ t('quality.daysShort', { n: 14 }) }}</el-radio-button>
        <el-radio-button :label="30">{{ t('quality.daysShort', { n: 30 }) }}</el-radio-button>
        <el-radio-button :label="90">{{ t('quality.daysShort', { n: 90 }) }}</el-radio-button>
      </el-radio-group>
      <el-button :loading="loading" @click="load">{{ t('quality.refresh') }}</el-button>
      <span v-if="report" class="window-label">
        {{ report.window.since }} → {{ report.window.until }}{{ t('quality.utc') }}
      </span>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="'读不到质量指标：' + error"
      class="mb-4"
    />

    <el-skeleton v-if="loading && !report" :rows="6" animated />

    <template v-if="report">
      <!-- The four numbers, each with the sample it was computed from. -->
      <div class="stat-grid">
        <div class="stat-card">
          <div class="stat-value">{{ formatRate(report.runs.success_rate) }}</div>
          <div class="stat-label">
            运行成功率 · 已结束 {{ report.runs.total - (report.runs.by_status.running || 0) }} 次
          </div>
          <div class="stat-hint">运行中被排除：它还没有结果，不是失败。</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ formatSeconds(report.runs.elapsed_ms.p95) }}</div>
          <div class="stat-label">P95 耗时 · 中位 {{ formatSeconds(report.runs.elapsed_ms.p50) }}</div>
          <div class="stat-hint">最慢 {{ formatSeconds(report.runs.elapsed_ms.max) }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ formatRate(report.jobs.success_rate) }}</div>
          <div class="stat-label">调度成功率 · 共 {{ report.jobs.total }} 次（含跳过）</div>
          <div class="stat-hint">跳过不算失败：它是有记录的一次不执行。</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ formatRate(report.approvals.acceptance_rate) }}</div>
          <div class="stat-label">
            草稿接受率 · 已决定
            {{ report.approvals.sent + report.approvals.rejected }} 份
          </div>
          <div class="stat-hint">{{ report.approvals.pending }} 份还在等人决定。</div>
        </div>
      </div>

      <!-- Runs -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">运行</span>
          <span class="section-note">
            {{ report.runs.total }} 次 · 工具调用 {{ report.runs.totals.tool_calls }} 次 ·
            {{ report.runs.totals.prompt_tokens }} / {{ report.runs.totals.completion_tokens }} tokens
          </span>
        </template>

        <el-empty v-if="!report.runs.total" :image-size="60"
          description="窗口内没有运行记录 —— 不是“全部成功”，是没有样本。" />

        <template v-else>
          <div class="tag-row">
            <el-tag v-for="(count, status) in report.runs.by_status" :key="status"
              :type="statusType(status)" size="small">
              {{ status }} {{ count }}
            </el-tag>
          </div>

          <div class="two-col">
            <div>
              <p class="section-label">终止原因（可分组的那一列）</p>
              <el-table :data="report.runs.by_stop_code" size="small">
                <el-table-column prop="value" label="stop_code" />
                <el-table-column prop="count" label="次数" width="80" align="right" />
              </el-table>
              <p class="hint">
                三种 <code>truncated</code>（时间/轮次/token）在这里是三个不同的值，
                在 <code>status</code> 里是同一个。
              </p>
            </div>
            <div>
              <p class="section-label">Prompt 版本</p>
              <el-table :data="report.runs.by_prompt_version" size="small">
                <el-table-column prop="value" label="指纹" />
                <el-table-column prop="count" label="次数" width="80" align="right" />
              </el-table>
              <p class="hint">
                改一个词，指纹就变。数字对不上时，先看这一列是不是换了版本。
              </p>
            </div>
          </div>
        </template>
      </el-card>

      <!-- Jobs -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">调度</span>
          <span class="section-note">{{ report.jobs.note }}</span>
        </template>

        <el-empty v-if="!report.jobs.total" :image-size="60"
          description="窗口内没有调度记录。" />

        <el-table v-else :data="report.jobs.by_job" size="small">
          <el-table-column prop="job_id" label="任务" />
          <el-table-column prop="total" label="总次数" width="90" align="right" />
          <el-table-column prop="succeeded" label="成功" width="80" align="right" />
          <el-table-column prop="failed" label="失败" width="80" align="right" />
          <el-table-column prop="skipped" label="跳过" width="80" align="right" />
          <el-table-column prop="abandoned" label="中断" width="80" align="right" />
        </el-table>
      </el-card>

      <!-- Closed loop -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">建议 → 回复</span>
          <span class="section-note">这封邮件是系统建议发的，它有没有被回复</span>
        </template>

        <el-alert :type="report.closed_loop.sent ? 'info' : 'warning'" :closable="false"
          show-icon :title="coverageSentence(report.closed_loop)" class="mb-3" />

        <div class="stat-mini-row">
          <div class="stat-mini">
            <span class="stat-mini-value">{{ formatCount(report.closed_loop.sent, '封') }}</span>
            <span class="stat-mini-label">已投递</span>
          </div>
          <div class="stat-mini">
            <span class="stat-mini-value">{{ formatCount(report.closed_loop.replied, '封') }}</span>
            <span class="stat-mini-label">被回复</span>
          </div>
          <div class="stat-mini">
            <span class="stat-mini-value">{{ formatRate(report.closed_loop.reply_rate) }}</span>
            <span class="stat-mini-label">回复率</span>
          </div>
          <div class="stat-mini">
            <span class="stat-mini-value">{{ formatRate(report.closed_loop.coverage) }}</span>
            <span class="stat-mini-label">可归因占比</span>
          </div>
        </div>

        <el-empty v-if="!report.closed_loop.sent" :image-size="60"
          description="窗口内没有已投递的邮件 —— 回复率没有分母，所以是 — 不是 0%。" />

        <template v-else>
          <div class="two-col">
            <div>
              <p class="section-label">按建议类型</p>
              <el-table :data="report.closed_loop.by_kind" size="small">
                <el-table-column label="建议">
                  <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
                </el-table-column>
                <el-table-column prop="sent" label="已投递" width="80" align="right" />
                <el-table-column prop="replied" label="回复" width="70" align="right" />
                <el-table-column label="回复率" width="90" align="right">
                  <template #default="{ row }">{{ formatRate(row.reply_rate) }}</template>
                </el-table-column>
              </el-table>
            </div>
            <div>
              <p class="section-label">按第几次跟进</p>
              <el-table :data="report.closed_loop.by_attempt" size="small">
                <el-table-column label="尝试">
                  <template #default="{ row }">{{ attemptLabel(row.attempt) }}</template>
                </el-table-column>
                <el-table-column prop="sent" label="已投递" width="80" align="right" />
                <el-table-column prop="replied" label="回复" width="70" align="right" />
                <el-table-column label="回复率" width="90" align="right">
                  <template #default="{ row }">{{ formatRate(row.reply_rate) }}</template>
                </el-table-column>
              </el-table>
            </div>
          </div>

          <p class="hint">{{ report.closed_loop.note }}</p>
        </template>

        <div class="excluded" v-if="report.closed_loop.excluded">
          未计入：
          <el-tag size="small" type="info" effect="plain">
            模拟发送 {{ report.closed_loop.excluded.simulated || 0 }}
          </el-tag>
          <el-tag size="small" type="info" effect="plain">
            退信 {{ report.closed_loop.excluded.bounced || 0 }}
          </el-tag>
          <el-tag size="small" type="info" effect="plain">
            发送失败 {{ report.closed_loop.excluded.failed || 0 }}
          </el-tag>
          <span class="hint hint-block">
            模拟发送没有经过 SMTP，不可能收到回复；把它算进分母会让回复率取决于是否配置了邮箱。
          </span>
        </div>

        <ul class="caveats">
          <li v-for="c in report.closed_loop.caveats" :key="c">{{ c }}</li>
        </ul>
      </el-card>

      <!-- Daily -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">按天</span>
          <span class="section-note">没有活动的日子也在表里 —— 缺失的一天看起来像好的一天</span>
        </template>

        <el-table :data="report.daily" size="small" max-height="360">
          <el-table-column prop="date" label="日期 (UTC)" width="120" />
          <el-table-column prop="runs_total" label="运行" width="80" align="right" />
          <el-table-column prop="runs_succeeded" label="成功" width="80" align="right" />
          <el-table-column prop="runs_failed" label="失败" width="80" align="right" />
          <el-table-column prop="runs_truncated" label="截断" width="80" align="right" />
          <el-table-column prop="jobs_total" label="调度" width="80" align="right" />
          <el-table-column prop="jobs_failed" label="调度失败" width="100" align="right" />
          <el-table-column label="" width="90">
            <template #default="{ row }">
              <span v-if="!dailyHasActivity(row)" class="quiet">无活动</span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <p class="generated">生成于 {{ report.generated_at }} · 只读聚合，不读取运行 trace</p>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { t } from '../i18n/index.js'
import { analyticsApi } from '../api'
import { runStatusType as statusType } from '../constants/status.js'
import {
  attemptLabel,
  coverageSentence,
  dailyHasActivity,
  formatCount,
  formatRate,
  formatSeconds,
  kindLabel,
} from '../utils/qualityFormat.js'

const days = ref(14)
const loading = ref(false)
const error = ref('')
const report = ref(null)

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    report.value = await analyticsApi.quality(days.value)
  } catch (e) {
    // Keep the last report on screen: a failed refresh should not look like an
    // empty window, which reads as "nothing ran".
    error.value = e.response?.data?.detail || e.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}
.window-label {
  color: var(--text-3);
  font-size: 12px;
}
/* Four tracks that may shrink (minmax(0, 1fr)), not four 220px minimums: the
   card labels are long Chinese sentences and a non-zero min track made the
   fourth card overflow instead of wrap. Below 1200px two columns are enough. */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}
@media (max-width: 1200px) {
  .stat-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
.stat-hint {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 6px;
}
.section {
  margin-bottom: 16px;
}
.section-title {
  font-weight: 600;
}
.section-note {
  margin-left: 12px;
  font-size: 12px;
  color: var(--text-3);
}
.section-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-2);
  margin: 8px 0;
}
.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
}
@media (max-width: 900px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}
.hint {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 6px;
  line-height: 1.6;
}
.stat-mini-row {
  display: flex;
  gap: 28px;
  margin-bottom: 12px;
}
.stat-mini-value {
  font-size: 20px;
  font-weight: 600;
  color: var(--text-1);
}
.stat-mini-label {
  font-size: 12px;
  color: var(--text-3);
  margin-left: 6px;
}
.excluded {
  margin-top: 12px;
  font-size: 12px;
  color: var(--text-2);
}
.excluded .el-tag {
  margin-right: 6px;
}
.caveats {
  margin: 12px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.7;
}
.quiet {
  color: var(--text-disabled);
  font-size: 12px;
}
.generated {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-3);
}
</style>
