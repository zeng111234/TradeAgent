<template>
  <div>
    <div class="page-header card-header-row">
      <div>
        <h2>数据看板</h2>
        <p>Overview of your foreign trade business</p>
      </div>
      <!-- Page-level actions. They used to live in the Daily Auto Report card
           header, which made the busiest card carry the page's whole command
           surface. -->
      <div class="page-header__actions">
        <el-button @click="runSearchNewLeads" :loading="scanAndSendLoading">
          <el-icon><Search /></el-icon> 搜索新客户
        </el-button>
        <el-button @click="runDailyReport" :loading="dailyLoading">
          <el-icon><DataAnalysis /></el-icon> Quick Report
        </el-button>
        <el-button type="primary" @click="runFullScan" :loading="fullScanLoading">
          <el-icon><MagicStick /></el-icon> Full Scan + Report
        </el-button>
      </div>
    </div>

    <!-- Four headline numbers, then the four that explain them. Eight cards at
         one weight was the page's loudest clutter; the month/task counts are
         context, not headlines. -->
    <div class="stat-grid mb-4">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total_customers }}</div>
        <div class="stat-label">客户总数</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.total_emails_sent }}</div>
        <div class="stat-label">邮件发送总量</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.open_rate }}%</div>
        <div class="stat-label">邮件打开率</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.reply_rate }}%</div>
        <div class="stat-label">邮件回复率</div>
      </div>
    </div>

    <div class="kpi-strip mb-6">
      <div class="kpi-strip__item">
        <div class="kpi-strip__value">{{ stats.new_customers_this_month }}</div>
        <div class="kpi-strip__label">本月新增客户</div>
      </div>
      <div class="kpi-strip__item">
        <div class="kpi-strip__value">{{ stats.emails_sent_this_month }}</div>
        <div class="kpi-strip__label">本月发送邮件</div>
      </div>
      <div class="kpi-strip__item">
        <div class="kpi-strip__value">{{ stats.pending_tasks }}</div>
        <div class="kpi-strip__label">待办任务</div>
      </div>
      <div class="kpi-strip__item">
        <div class="kpi-strip__value" :class="{ 'kpi-strip__value--alert': stats.overdue_tasks > 0 }">
          {{ stats.overdue_tasks }}
        </div>
        <div class="kpi-strip__label">逾期任务</div>
      </div>
    </div>

    <!-- Charts -->
    <el-row :gutter="20" class="mb-6">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">销售漏斗</span>
          </template>
          <div ref="funnelChartRef" class="chart-box"></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">月度趋势</span>
          </template>
          <div ref="trendChartRef" class="chart-box"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">客户来源分布</span>
          </template>
          <div ref="sourceChartRef" class="chart-box"></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">国家/地区分布</span>
          </template>
          <div ref="countryChartRef" class="chart-box"></div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Daily Report / Scheduler -->
    <el-row :gutter="20" class="mt-6">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">Daily Auto Report</span>
          </template>
          <div v-if="dailyLoading || fullScanLoading || scanAndSendLoading" class="report-loading">
            <el-icon class="is-loading" :size="24"><Loading /></el-icon>
            <p class="mt-2">{{ scanAndSendLoading ? 'Searching web + generating AI emails + sending... This may take 2-3 minutes.' : fullScanLoading ? 'Scanning web + generating AI drafts... This may take 1-2 minutes.' : 'Analyzing your CRM data...' }}</p>
          </div>
          <div v-else-if="dailyReport" class="report-body">{{ dailyReport }}</div>
          <el-empty v-else description="Click 'Quick Report' for CRM analysis or 'Full Scan' to search for new leads" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>
            <span class="card-title">Scheduled Jobs</span>
          </template>
          <div v-for="job in schedulerJobs" :key="job.id" class="job-row">
            <div class="job-name">{{ job.name }}</div>
            <div class="hint">{{ humanTrigger(job.trigger) }}</div>
            <div class="hint text-positive">Next: {{ humanNextRun(job.next_run) }}</div>
          </div>
          <el-empty v-if="!schedulerJobs.length" description="No scheduled jobs" />
        </el-card>
      </el-col>
    </el-row>

    <!-- New Leads Search Results & Review -->
    <el-row :gutter="20" class="mt-6" v-if="searchedLeads.length > 0">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header>
            <div class="card-header-row">
              <div>
                <span class="card-title">New Leads Found</span>
                <span class="section-note ml-2">Review AI-generated emails and select which to send</span>
              </div>
              <div>
                <el-button @click="toggleSelectAllLeads" size="default">
                  {{ selectedLeadIndices.size > 0 ? 'Deselect All' : 'Select All' }}
                </el-button>
                <el-button @click="sendSelectedDrafts" :loading="sendDraftsLoading" size="large">
                  <el-icon><Promotion /></el-icon> Send Selected ({{ selectedLeadsCount }})
                </el-button>
              </div>
            </div>
          </template>

          <div v-if="sendDraftsResult" class="mb-3">
            <!-- The count alone read as success even when half the batch failed.
                 The API has always returned emails_failed and send_results; the
                 banner now says so, and names the ones that did not go out. -->
            <el-alert
              :title="`Sent ${sendDraftsResult.emails_sent} emails, saved ${sendDraftsResult.crm_saved} to CRM`
                + (sendDraftsResult.emails_failed ? `, ${sendDraftsResult.emails_failed} failed` : '')"
              :type="sendDraftsResult.emails_failed ? 'warning' : 'success'"
              show-icon :closable="true" />
            <el-alert
              v-if="failedDrafts.length"
              class="mt-2"
              type="error" :closable="false" show-icon
              :title="`Not sent: ${failedDrafts.map(f => `${f.company || f.email}: ${f.error || 'unknown error'}`).join(' · ')}`" />
          </div>

          <!-- Column budget 890 (was 1190): fixed 340 + elastic 550. The old
               widths overflowed the 1138px available at 1440 and put a
               horizontal scrollbar inside the card. -->
          <el-table :data="searchedLeads" stripe size="small" max-height="500">
            <el-table-column label="Send" width="48" align="center">
              <template #default="{ row, $index }">
                <el-checkbox
                  :model-value="selectedLeadIndices.has($index)"
                  :disabled="!row.email || row.already_exists"
                  @change="toggleLeadSelection($index)"
                />
              </template>
            </el-table-column>
            <el-table-column prop="company_name" label="Company" min-width="170" show-overflow-tooltip />
            <el-table-column prop="country" label="Country" width="88" show-overflow-tooltip />
            <el-table-column label="Score" width="64" align="center">
              <template #default="{ row }">
                <el-tag :type="scoreTagType(row.relevance_score)" size="small">
                  {{ row.relevance_score }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="Email" min-width="160">
              <template #default="{ row }">
                <el-tooltip v-if="row.email" :content="row.email" placement="top">
                  <span class="cell-truncate">{{ row.email }}</span>
                </el-tooltip>
                <span v-else class="text-muted">No email</span>
              </template>
            </el-table-column>
            <el-table-column label="Status" width="88" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.already_exists" type="warning" size="small">Exists</el-tag>
                <el-tag v-else-if="!row.email" type="info" size="small">No email</el-tag>
                <el-tag v-else type="success" size="small">Ready</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="AI Draft Email" min-width="220">
              <template #default="{ row }">
                <el-popover trigger="hover" width="450" placement="left">
                  <template #reference>
                    <span class="cell-link">{{ (row.draft_body || '').substring(0, 80) }}...</span>
                  </template>
                  <div class="popover-body">
                    <div class="popover-subject">Subject: {{ row.draft_subject }}</div>
                    {{ row.draft_body }}
                  </div>
                </el-popover>
              </template>
            </el-table-column>
            <el-table-column label="Website" width="52" align="center">
              <template #default="{ row }">
                <a v-if="row.website" :href="row.website" target="_blank" class="cell-link">Visit</a>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <!-- Import Daily Scan Leads -->
    <el-row :gutter="20" class="mt-6">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header>
            <div class="card-header-row">
              <div>
                <span class="card-title">Import Daily Scan Leads</span>
                <span class="section-note ml-2">Import leads from daily_scan.py into CRM + draft emails</span>
              </div>
              <div>
                <el-button @click="previewLeads" :loading="previewLoading" size="default">
                  <el-icon><View /></el-icon> Preview
                </el-button>
                <el-button @click="importLeads" :loading="importLoading" size="default">
                  <el-icon><Download /></el-icon> Import All
                </el-button>
              </div>
            </div>
          </template>

          <div v-if="importResult" class="mb-3">
            <el-alert :title="`Imported ${importResult.imported} customers, ${importResult.skipped} skipped (already exist)`" type="success" show-icon :closable="true" />
          </div>

          <div v-if="previewData && previewData.leads && previewData.leads.length">
            <!-- Column budget 814 (was 1240). Website folded into Company and
                 Draft/Preview merged into one cell -- two columns that only
                 ever showed a link and a truncation. -->
            <el-table :data="previewData.leads" stripe size="small" max-height="400">
              <el-table-column label="Company" min-width="220">
                <template #default="{ row }">
                  <div class="cell-stack">
                    <span class="cell-truncate" :title="row.company_name">{{ row.company_name }}</span>
                    <a v-if="row.website" :href="row.website" target="_blank" class="cell-link">Website</a>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="country" label="Country" width="88" show-overflow-tooltip />
              <el-table-column prop="keyword" label="Keyword" width="120" show-overflow-tooltip />
              <el-table-column label="Score" width="64" align="center">
                <template #default="{ row }">
                  <el-tag :type="scoreTagType(row.relevance_score)" size="small">
                    {{ row.relevance_score }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="Emails" min-width="160">
                <template #default="{ row }">
                  <span v-if="row.emails && row.emails.length" class="cell-truncate" :title="row.emails.join(', ')">
                    {{ row.emails.join(', ') }}
                  </span>
                  <span v-else class="text-muted">No email found</span>
                </template>
              </el-table-column>
              <el-table-column label="Draft" min-width="162">
                <template #default="{ row }">
                  <el-popover v-if="row.draft_email" trigger="hover" width="400" placement="left">
                    <template #reference>
                      <span class="cell-draft">
                        <el-tag type="success" size="small">Ready</el-tag>
                        <span class="cell-link">{{ (row.draft_email || '').substring(0, 40) }}...</span>
                      </span>
                    </template>
                    <div class="popover-body popover-body--short">{{ row.draft_email }}</div>
                  </el-popover>
                  <el-tag v-else type="info" size="small">None</el-tag>
                </template>
              </el-table-column>
            </el-table>
            <div class="meta mt-3">
              Scan date: {{ previewData.scan_date || 'N/A' }} | Keywords: {{ (previewData.keywords_used || []).join(', ') }}
            </div>
          </div>

          <el-empty v-else-if="!previewLoading" description="No scan results found. Run daily_scan.py first to generate leads." />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import { analyticsApi, schedulerApi, agentApi } from '../api'
import { scoreTagType } from '../constants/status.js'
import { accent, accentSeries, trendSeries, accentSoft, white } from '../constants/theme.js'

const stats = reactive({
  total_customers: 0,
  new_customers_this_month: 0,
  total_emails_sent: 0,
  emails_sent_this_month: 0,
  open_rate: 0,
  reply_rate: 0,
  pending_tasks: 0,
  overdue_tasks: 0,
})

const funnelChartRef = ref(null)
const trendChartRef = ref(null)
const sourceChartRef = ref(null)
const countryChartRef = ref(null)

let charts = []

const initCharts = async () => {
  await nextTick()

  // Funnel chart
  if (funnelChartRef.value) {
    const funnelChart = echarts.init(funnelChartRef.value)
    charts.push(funnelChart)
    try {
      const pipeline = await analyticsApi.pipeline()
      // Width is proportional to count, so `max` must be the *largest stage* --
      // it used to be `pipeline.total` (the sum), which shrank every band to
      // 1/n and turned the funnel into a thin ribbon. Stages with no rows are
      // dropped: a zero-width band still paints its label in mid-air.
      const stages = pipeline.stages.filter(s => s.count > 0)
      funnelChart.setOption({
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        // One hue, eight lightness steps: eight unrelated hues is the rainbow
        // this page is losing, and a greyscale pie makes neighbours identical.
        color: accentSeries,
        series: [{
          type: 'funnel',
          left: '10%',
          top: 20,
          bottom: 20,
          width: '80%',
          min: 0,
          max: Math.max(...stages.map(s => s.count), 1),
          minSize: '0%',
          maxSize: '100%',
          sort: 'descending',
          gap: 2,
          label: { show: true, position: 'inside', formatter: '{b}\n{c}' },
          data: stages.map(s => ({ name: s.stage, value: s.count })),
        }],
      })
    } catch (e) {
      console.error('Pipeline chart error:', e)
    }
  }

  // Trend chart
  if (trendChartRef.value) {
    const trendChart = echarts.init(trendChartRef.value)
    charts.push(trendChart)
    try {
      const trends = await analyticsApi.trends(6)
      trendChart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: ['新增客户', '发送邮件', '邮件打开', '邮件回复'], bottom: 0 },
        grid: { left: '3%', right: '4%', bottom: '12%', top: '5%', containLabel: true },
        xAxis: { type: 'category', data: trends.map(t => t.month) },
        yAxis: { type: 'value' },
        series: [
          { name: '新增客户', type: 'line', smooth: true, data: trends.map(t => t.customers_added), areaStyle: { opacity: 0.1 }, itemStyle: { color: trendSeries[0] } },
          { name: '发送邮件', type: 'line', smooth: true, data: trends.map(t => t.emails_sent), areaStyle: { opacity: 0.1 }, itemStyle: { color: trendSeries[1] } },
          { name: '邮件打开', type: 'line', smooth: true, data: trends.map(t => t.emails_opened), itemStyle: { color: trendSeries[2] } },
          { name: '邮件回复', type: 'line', smooth: true, data: trends.map(t => t.emails_replied), itemStyle: { color: trendSeries[3] } },
        ],
      })
    } catch (e) {
      console.error('Trend chart error:', e)
    }
  }

  // Source pie chart
  if (sourceChartRef.value) {
    const sourceChart = echarts.init(sourceChartRef.value)
    charts.push(sourceChart)
    try {
      const sources = await analyticsApi.sources()
      sourceChart.setOption({
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        // Five slices taken from spread-out steps of the ramp: adjacent steps
        // of a single hue are hard to tell apart in a ring.
        color: [accentSeries[0], accentSeries[2], accentSeries[4], accentSeries[6], accentSeries[7]],
        legend: { orient: 'vertical', left: 'left', top: 'center' },
        series: [{
          type: 'pie',
          radius: ['40%', '70%'],
          center: ['60%', '50%'],
          avoidLabelOverlap: false,
          itemStyle: { borderRadius: 6, borderColor: white, borderWidth: 2 },
          label: { show: true, formatter: '{b}\n{d}%' },
          data: sources.map(s => ({ name: s.source, value: s.count })),
        }],
      })
    } catch (e) {
      console.error('Source chart error:', e)
    }
  }

  // Country bar chart
  if (countryChartRef.value) {
    const countryChart = echarts.init(countryChartRef.value)
    charts.push(countryChart)
    try {
      const countries = await analyticsApi.countries()
      countryChart.setOption({
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: '3%', right: '4%', bottom: '3%', top: '5%', containLabel: true },
        xAxis: { type: 'value' },
        yAxis: { type: 'category', data: countries.map(c => c.country).reverse(), axisLabel: { width: 80, overflow: 'truncate' } },
        series: [{
          type: 'bar',
          data: countries.map(c => c.count).reverse(),
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
              { offset: 0, color: accentSoft },
              { offset: 1, color: accent },
            ]),
            borderRadius: [0, 4, 4, 0],
          },
        }],
      })
    } catch (e) {
      console.error('Country chart error:', e)
    }
  }
}

const handleResize = () => {
  charts.forEach(c => c.resize())
}

// Scheduler
const dailyLoading = ref(false)
const fullScanLoading = ref(false)
const scanAndSendLoading = ref(false)
const dailyReport = ref(null)
const schedulerJobs = ref([])

/* The scheduler hands back its own repr -- `cron[hour='1,7', minute='7']` and
 * `2026-10-21 07:08:38.557990+00:00`. Neither is a thing to read; convert to
 * "Daily at 01:07, 07:07" and "10-21 15:08" (local). Unknown shapes fall back
 * to the raw string rather than lying. */
const humanTrigger = (raw) => {
  const hour = /hour=['"]?([\d,]+)['"]?/.exec(raw || '')?.[1]
  const minute = /minute=['"]?([\d,]+)['"]?/.exec(raw || '')?.[1]
  if (!hour || !minute) return raw || ''
  const pad = (n) => String(n).padStart(2, '0')
  const times = hour.split(',').map((h) => `${pad(h)}:${pad(minute.split(',')[0])}`)
  return `Daily at ${times.join(', ')}`
}

const humanNextRun = (raw) => {
  if (!raw) return '—'
  const iso = String(raw).replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1')
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? raw
    : d.toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

// Import Leads
const previewLoading = ref(false)
const importLoading = ref(false)
const previewData = ref(null)
const importResult = ref(null)

const previewLeads = async () => {
  previewLoading.value = true
  try {
    const data = await agentApi.previewLeads()
    previewData.value = data
    if (!data.leads || !data.leads.length) {
      ElMessage.info('No scan results found. Run daily_scan.py first.')
    }
  } catch (e) {
    ElMessage.error('Preview failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    previewLoading.value = false
  }
}

const importLeads = async () => {
  importLoading.value = true
  try {
    const result = await agentApi.importLeads()
    importResult.value = result
    if (result.imported > 0) {
      ElMessage.success(`Imported ${result.imported} customers with draft emails! Check Emails > Pending Review.`)
      // Refresh preview
      await previewLeads()
    } else {
      ElMessage.info(`No new leads to import. ${result.skipped} already exist in database.`)
    }
  } catch (e) {
    ElMessage.error('Import failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    importLoading.value = false
  }
}

const runDailyReport = async () => {
  // Quick report: analyze existing CRM data only (no web scanning, fast)
  dailyLoading.value = true
  dailyReport.value = null
  try {
    const [dashData, followUps, churnResult] = await Promise.all([
      analyticsApi.dashboard(),
      agentApi.dailyIntelligence(),
      agentApi.churnAlerts(),
    ])

    const lines = []
    const today = new Date().toISOString().split('T')[0]
    lines.push(`Daily CRM Report - ${today}`)
    lines.push('=' .repeat(50))
    lines.push('')

    // Customer overview
    lines.push(`[Customer Overview]`)
    lines.push(`  Total customers: ${dashData.total_customers || 0}`)
    lines.push(`  New this month: ${dashData.new_customers_this_month || 0}`)
    lines.push(`  Pending tasks: ${dashData.pending_tasks || 0}`)
    lines.push(`  Overdue tasks: ${dashData.overdue_tasks || 0}`)
    lines.push('')

    // Email stats
    lines.push(`[Email Stats]`)
    lines.push(`  Total sent: ${dashData.total_emails_sent || 0}`)
    lines.push(`  Open rate: ${dashData.open_rate || 0}%`)
    lines.push(`  Reply rate: ${dashData.reply_rate || 0}%`)
    lines.push('')

    // Churn alerts
    const crit = churnResult?.critical || 0
    const high = churnResult?.high || 0
    if (crit > 0 || high > 0) {
      lines.push(`[Customer Alerts] ${crit} critical, ${high} high-risk:`)
      ;(churnResult?.alerts || []).slice(0, 5).forEach(a => {
        lines.push(`  - [${(a.risk_level || '').toUpperCase()}] ${a.company_name}: ${a.risk_reasons?.[0] || 'At risk'}`)
      })
    } else {
      lines.push('[Customer Alerts] All customers look healthy.')
    }
    lines.push('')

    // Follow-ups
    const followActions = followUps?.actions || []
    if (followActions.length > 0) {
      lines.push(`[Follow-ups Needed] ${followUps?.high_priority || 0} high priority:`)
      followActions.slice(0, 5).forEach(f => {
        lines.push(`  - ${f.company_name}: ${f.suggested_action || 'Follow up'}`)
      })
    } else {
      lines.push('[Follow-ups] No urgent follow-ups needed today.')
    }
    lines.push('')
    lines.push('Tip: Use "Full Scan + Report" to search for new leads online.')

    dailyReport.value = lines.join('\n')
  } catch (e) {
    ElMessage.error('Report failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    dailyLoading.value = false
  }
}

const runFullScan = async () => {
  // Full scan: search web + generate AI drafts (slow, 1-2 min)
  fullScanLoading.value = true
  dailyReport.value = null
  try {
    const result = await schedulerApi.runDaily({ search_keywords: 'textile buyer' })
    const lines = []
    lines.push(`Full Scan Report - ${result.date || new Date().toISOString().split('T')[0]}`)
    lines.push('=' .repeat(50))
    lines.push('')

    lines.push(`[New Leads] Found ${result.leads_count || 0} potential customers:`)
    if (result.new_leads && result.new_leads.length) {
      result.new_leads.slice(0, 5).forEach(l => {
        const emails = l.emails && l.emails.length ? ` | Email: ${l.emails[0]}` : ''
        lines.push(`  - ${l.company_name} (${l.country}) Score: ${l.relevance_score}${emails}`)
      })
    } else {
      lines.push('  No new leads found today.')
    }

    if (result.draft_emails && result.draft_emails.length) {
      lines.push('')
      lines.push('[Draft Emails] AI generated for new leads:')
      result.draft_emails.forEach(d => {
        lines.push(`  - ${d.company_name}:`)
        lines.push(`    Subject: ${d.subject}`)
        lines.push(`    Body: ${(d.body_preview || '').substring(0, 100)}...`)
      })
    }
    lines.push('')

    const crit = result.critical_count || 0
    const high = result.high_count || 0
    if (crit > 0 || high > 0) {
      lines.push(`[Alerts] ${crit} critical, ${high} high-risk customers:`)
      ;(result.churn_alerts || []).slice(0, 3).forEach(a => {
        lines.push(`  - [${(a.risk_level || '').toUpperCase()}] ${a.company_name}: ${a.risk_reasons?.[0] || 'Unknown'}`)
      })
    } else {
      lines.push('[Alerts] No customer churn alerts.')
    }

    dailyReport.value = lines.join('\n')
    ElMessage.success('Full scan completed! Leads with AI draft emails are ready.')
  } catch (e) {
    ElMessage.error('Full scan failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    fullScanLoading.value = false
  }
}

// New Leads Search & Review
const searchedLeads = ref([])
const selectedLeadIndices = ref(new Set())
const sendDraftsLoading = ref(false)
const sendDraftsResult = ref(null)

// Up to five failed sends, named. The response carries one entry per selected
// lead with its status and error; showing only the total made a half-failed
// batch look like a clean success.
const failedDrafts = computed(() =>
  (sendDraftsResult.value?.send_results || [])
    .filter(r => r.status === 'failed')
    .slice(0, 5)
)

const runSearchNewLeads = async () => {
  scanAndSendLoading.value = true
  searchedLeads.value = []
  selectedLeadIndices.value = new Set()
  sendDraftsResult.value = null
  dailyReport.value = null
  try {
    const result = await agentApi.scanLeadsDraft({
      product_keywords: 'embroidery thread, gold metallic yarn',
      target_country: '',
      max_results: 10,
    })

    const leads = result.leads || []
    searchedLeads.value = leads

    // Auto-select all leads that have email and don't already exist
    leads.forEach((l, i) => {
      if (l.email && !l.already_exists) {
        selectedLeadIndices.value.add(i)
      }
    })
    selectedLeadIndices.value = new Set(selectedLeadIndices.value)

    if (leads.length > 0) {
      ElMessage.success(`Found ${result.total_found} leads, ${result.with_email} with email. Review below and select which to send.`)
    } else {
      ElMessage.info(`Found ${result.total_found} leads but none have email addresses.`)
    }
  } catch (e) {
    ElMessage.error('Search failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    scanAndSendLoading.value = false
  }
}

const toggleLeadSelection = (index) => {
  if (selectedLeadIndices.value.has(index)) {
    selectedLeadIndices.value.delete(index)
  } else {
    selectedLeadIndices.value.add(index)
  }
  selectedLeadIndices.value = new Set(selectedLeadIndices.value)
}

const toggleSelectAllLeads = () => {
  const eligible = searchedLeads.value
    .map((l, i) => ({ lead: l, index: i }))
    .filter(({ lead }) => lead.email && !lead.already_exists)

  if (eligible.length === selectedLeadIndices.value.size) {
    selectedLeadIndices.value = new Set()
  } else {
    selectedLeadIndices.value = new Set(eligible.map(({ index }) => index))
  }
}

const selectedLeadsCount = computed(() => {
  return Array.from(selectedLeadIndices.value).filter(i => {
    const l = searchedLeads.value[i]
    return l && l.email && !l.already_exists
  }).length
})

const sendSelectedDrafts = async () => {
  const selected = Array.from(selectedLeadIndices.value)
    .map(i => searchedLeads.value[i])
    .filter(l => l && l.email && !l.already_exists)

  if (selected.length === 0) {
    ElMessage.warning('No leads selected for sending')
    return
  }

  sendDraftsLoading.value = true
  sendDraftsResult.value = null
  try {
    const result = await agentApi.sendDraftedEmails({
      selected_leads: selected,
      product_keywords: 'embroidery thread, gold metallic yarn',
    })

    sendDraftsResult.value = result

    if (result.emails_sent > 0) {
      ElMessage.success(`Sent ${result.emails_sent} emails, saved ${result.crm_saved} to CRM!`)
    } else {
      ElMessage.info('No emails were sent. Check the result for details.')
    }
  } catch (e) {
    ElMessage.error('Send failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    sendDraftsLoading.value = false
  }
}

onMounted(async () => {
  try {
    const data = await analyticsApi.dashboard()
    Object.assign(stats, data)
  } catch (e) {
    console.error('Dashboard stats error:', e)
  }
  // Load scheduler jobs
  try {
    const jobData = await schedulerApi.getJobs()
    schedulerJobs.value = jobData.jobs || []
  } catch (e) { /* ignore */ }
  await initCharts()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  charts.forEach(c => c.dispose())
})
</script>