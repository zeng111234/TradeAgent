<template>
  <div>
    <div class="page-header">
      <h2>数据看板</h2>
      <p>Overview of your foreign trade business</p>
    </div>

    <!-- Stats Cards -->
    <el-row :gutter="20" style="margin-bottom: 24px">
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value">{{ stats.total_customers }}</div>
          <div class="stat-label">客户总数</div>
          <el-icon class="stat-icon" color="#409eff"><User /></el-icon>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value">{{ stats.new_customers_this_month }}</div>
          <div class="stat-label">本月新增客户</div>
          <el-icon class="stat-icon" color="#67c23a"><Plus /></el-icon>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value">{{ stats.total_emails_sent }}</div>
          <div class="stat-label">邮件发送总量</div>
          <el-icon class="stat-icon" color="#e6a23c"><Message /></el-icon>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value">{{ stats.open_rate }}%</div>
          <div class="stat-label">邮件打开率</div>
          <el-icon class="stat-icon" color="#f56c6c"><TrendCharts /></el-icon>
        </div>
      </el-col>
    </el-row>

    <!-- Second Row -->
    <el-row :gutter="20" style="margin-bottom: 24px">
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value">{{ stats.reply_rate }}%</div>
          <div class="stat-label">邮件回复率</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value">{{ stats.emails_sent_this_month }}</div>
          <div class="stat-label">本月发送邮件</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value">{{ stats.pending_tasks }}</div>
          <div class="stat-label">待办任务</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value" :style="{ color: stats.overdue_tasks > 0 ? '#f56c6c' : '#303133' }">
            {{ stats.overdue_tasks }}
          </div>
          <div class="stat-label">逾期任务</div>
        </div>
      </el-col>
    </el-row>

    <!-- Charts -->
    <el-row :gutter="20" style="margin-bottom: 24px">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span style="font-weight: 600">销售漏斗</span>
          </template>
          <div ref="funnelChartRef" style="height: 350px"></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span style="font-weight: 600">月度趋势</span>
          </template>
          <div ref="trendChartRef" style="height: 350px"></div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span style="font-weight: 600">客户来源分布</span>
          </template>
          <div ref="sourceChartRef" style="height: 350px"></div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <span style="font-weight: 600">国家/地区分布</span>
          </template>
          <div ref="countryChartRef" style="height: 350px"></div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Daily Report / Scheduler -->
    <el-row :gutter="20" style="margin-top: 24px">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">Daily Auto Report</span>
              <el-button type="primary" @click="runDailyReport" :loading="dailyLoading" size="large">
                <el-icon><MagicStick /></el-icon> Run Now
              </el-button>
            </div>
          </template>
          <div v-if="dailyReport" style="white-space: pre-wrap; font-family: monospace; font-size: 13px; color: #303133; background: #f5f7fa; padding: 16px; border-radius: 6px; line-height: 1.8">{{ dailyReport }}</div>
          <el-empty v-else description="Click 'Run Now' to generate the daily report" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>
            <span style="font-weight: 600">Scheduled Jobs</span>
          </template>
          <div v-for="job in schedulerJobs" :key="job.id" style="border-bottom: 1px solid #eee; padding: 8px 0;">
            <div style="font-weight: 600; font-size: 13px">{{ job.name }}</div>
            <div style="font-size: 11px; color: #909399">{{ job.trigger }}</div>
            <div style="font-size: 11px; color: #67c23a">Next: {{ job.next_run }}</div>
          </div>
          <el-empty v-if="!schedulerJobs.length" description="No scheduled jobs" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import { analyticsApi, schedulerApi } from '../api'

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
      funnelChart.setOption({
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        color: ['#409eff', '#67c23a', '#e6a23c', '#f56c6c', '#909399', '#b37feb', '#36cfc9', '#ff85c0'],
        series: [{
          type: 'funnel',
          left: '10%',
          top: 20,
          bottom: 20,
          width: '80%',
          min: 0,
          max: pipeline.total || 100,
          minSize: '0%',
          maxSize: '100%',
          sort: 'descending',
          gap: 2,
          label: { show: true, position: 'inside', formatter: '{b}\n{c}' },
          data: pipeline.stages.map(s => ({ name: s.stage, value: s.count })),
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
          { name: '新增客户', type: 'line', smooth: true, data: trends.map(t => t.customers_added), areaStyle: { opacity: 0.1 } },
          { name: '发送邮件', type: 'line', smooth: true, data: trends.map(t => t.emails_sent), areaStyle: { opacity: 0.1 } },
          { name: '邮件打开', type: 'line', smooth: true, data: trends.map(t => t.emails_opened) },
          { name: '邮件回复', type: 'line', smooth: true, data: trends.map(t => t.emails_replied) },
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
        legend: { orient: 'vertical', left: 'left', top: 'center' },
        series: [{
          type: 'pie',
          radius: ['40%', '70%'],
          center: ['60%', '50%'],
          avoidLabelOverlap: false,
          itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
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
          itemStyle: { color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#409eff' },
            { offset: 1, color: '#67c23a' },
          ]), borderRadius: [0, 4, 4, 0] },
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
const dailyReport = ref(null)
const schedulerJobs = ref([])

const runDailyReport = async () => {
  dailyLoading.value = true
  try {
    const result = await schedulerApi.runDaily({ search_keywords: 'textile buyer' })
    // Format the result as a readable report
    const lines = []
    lines.push(`Good morning! Report for ${result.date || new Date().toISOString().split('T')[0]}:`)
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
    // Show draft emails if any
    if (result.draft_emails && result.draft_emails.length) {
      lines.push('')
      lines.push('[Draft Emails] AI generated for new leads:')
      result.draft_emails.forEach(d => {
        lines.push(`  - ${d.company_name}:`)
        lines.push(`    Subject: ${d.subject}`)
        lines.push(`    Body: ${d.body_preview || '(click to view full draft)'}`)
      })
    }
    lines.push('')
    const crit = result.critical_count || 0
    const high = result.high_count || 0
    if (crit > 0 || high > 0) {
      lines.push(`[Alerts] ${crit} critical, ${high} high-risk customers:`)
      ;(result.churn_alerts || []).slice(0, 3).forEach(a => {
        lines.push(`  - [${a.risk_level.toUpperCase()}] ${a.company_name}: ${a.risk_reasons?.[0] || 'Unknown'}`)
      })
    } else {
      lines.push('[Alerts] No customer churn alerts.')
    }
    lines.push('')
    const fu = result.high_priority_followups || 0
    if (fu > 0) {
      lines.push(`[Follow-ups] ${fu} customers need follow-up today:`)
      ;(result.follow_ups || []).slice(0, 3).forEach(f => {
        lines.push(`  - ${f.company_name}: ${f.suggested_action}`)
      })
    } else {
      lines.push('[Follow-ups] No urgent follow-ups today.')
    }
    dailyReport.value = lines.join('\n')
  } catch (e) {
    ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    dailyLoading.value = false
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