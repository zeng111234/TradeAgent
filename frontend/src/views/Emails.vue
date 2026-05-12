<template>
  <div>
    <div class="page-header">
      <h2>邮件营销</h2>
      <p>Send emails, track opens, and manage your email campaigns</p>
    </div>

    <el-row :gutter="20" style="margin-bottom: 24px">
      <!-- Email Stats -->
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value">{{ emailStats.total_sent }}</div>
          <div class="stat-label">Total Sent</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value" style="color: #67c23a">{{ emailStats.open_rate }}%</div>
          <div class="stat-label">Open Rate</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value" style="color: #409eff">{{ emailStats.reply_rate }}%</div>
          <div class="stat-label">Reply Rate</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card">
          <div class="stat-value" style="color: #f56c6c">{{ emailStats.total_failed }}</div>
          <div class="stat-label">Failed</div>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="20">
      <!-- Send Email Form -->
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>
            <span style="font-weight: 600">Send Email</span>
          </template>
          <el-form :model="sendForm" label-width="100px">
            <el-form-item label="To Email" required>
              <el-input v-model="sendForm.to_email" placeholder="recipient@example.com" />
            </el-form-item>
            <el-form-item label="To Name">
              <el-input v-model="sendForm.to_name" placeholder="Recipient name" />
            </el-form-item>
            <el-form-item label="Template">
              <el-select v-model="sendForm.template_id" style="width: 100%" clearable @change="onTemplateSelect">
                <el-option v-for="t in templates" :key="t.id" :label="t.name" :value="t.id" />
              </el-select>
            </el-form-item>
            <el-form-item label="Subject" required>
              <el-input v-model="sendForm.subject" :disabled="!!sendForm.template_id" />
            </el-form-item>
            <el-form-item label="Body" required>
              <el-input v-model="sendForm.body" type="textarea" :rows="6" :disabled="!!sendForm.template_id" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="handleSend" :loading="sending">
                <el-icon><Promotion /></el-icon> Send Email
              </el-button>
              <el-button @click="handleGenerateAI">
                <el-icon><MagicStick /></el-icon> AI Generate
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>

      <!-- AI Generate Dialog Inline -->
      <el-col :span="14">
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header>
            <span style="font-weight: 600">AI Email Generator</span>
          </template>
          <el-form :model="aiForm" label-width="120px">
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="Product Name" required>
                  <el-input v-model="aiForm.product_name" placeholder="e.g. LED Panel Lights" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="Your Company">
                  <el-input v-model="aiForm.company_name" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="12">
              <el-col :span="12">
                <el-form-item label="Target Industry">
                  <el-input v-model="aiForm.target_industry" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="Target Country">
                  <el-input v-model="aiForm.target_country" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="Selling Points">
              <el-input v-model="aiForm.selling_points" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="generateEmail" :loading="generating">
                <el-icon><MagicStick /></el-icon> Generate Email
              </el-button>
            </el-form-item>
          </el-form>

          <!-- AI Result -->
          <div v-if="aiResult" style="background: #f5f7fa; padding: 16px; border-radius: 8px; margin-top: 12px">
            <h4 style="margin: 0 0 8px">Generated Email:</h4>
            <p><strong>Subject:</strong> {{ aiResult.subject }}</p>
            <div v-html="aiResult.body" style="margin: 8px 0; color: #606266"></div>
            <el-button size="small" type="success" @click="useAiResult">
              Use This Email
            </el-button>
          </div>
        </el-card>

        <!-- Email Logs -->
        <el-card shadow="never">
          <template #header>
            <span style="font-weight: 600">Recent Email Logs</span>
          </template>
          <el-table :data="emailLogs" stripe>
            <el-table-column prop="to_email" label="To" min-width="150" />
            <el-table-column prop="subject" label="Subject" min-width="200" show-overflow-tooltip />
            <el-table-column prop="status" label="Status" width="100">
              <template #default="{ row }">
                <el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="open_count" label="Opens" width="70" align="center" />
            <el-table-column label="Time" width="160">
              <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { emailApi } from '../api'

const sending = ref(false)
const generating = ref(false)
const templates = ref([])
const emailLogs = ref([])
const aiResult = ref(null)

const emailStats = reactive({
  total_sent: 0,
  total_opened: 0,
  total_replied: 0,
  total_failed: 0,
  open_rate: 0,
  reply_rate: 0,
})

const sendForm = reactive({
  to_email: '',
  to_name: '',
  template_id: null,
  subject: '',
  body: '',
})

const aiForm = reactive({
  product_name: '',
  company_name: '',
  target_industry: '',
  target_country: '',
  selling_points: '',
  tone: 'professional',
  language: 'en',
})

const statusType = (status) => {
  const map = { sent: 'success', opened: 'success', replied: 'success', failed: 'danger', bounced: 'danger', pending: 'info' }
  return map[status] || ''
}

const formatDate = (d) => d ? new Date(d).toLocaleString() : ''

const loadData = async () => {
  try {
    const [stats, logs, tmpls] = await Promise.all([
      emailApi.getStats(),
      emailApi.getLogs(),
      emailApi.getTemplates(),
    ])
    Object.assign(emailStats, stats)
    emailLogs.value = logs
    templates.value = tmpls
  } catch (e) {
    console.error('Load error:', e)
  }
}

const onTemplateSelect = (id) => {
  if (id) {
    const tmpl = templates.value.find(t => t.id === id)
    if (tmpl) {
      sendForm.subject = tmpl.subject
      sendForm.body = tmpl.body
    }
  } else {
    sendForm.subject = ''
    sendForm.body = ''
  }
}

const handleSend = async () => {
  if (!sendForm.to_email) { ElMessage.warning('Email is required'); return }
  sending.value = true
  try {
    await emailApi.send(sendForm)
    ElMessage.success('Email sent successfully!')
    sendForm.to_email = ''
    sendForm.to_name = ''
    loadData()
  } catch (e) {
    ElMessage.error('Failed to send email')
  } finally {
    sending.value = false
  }
}

const generateEmail = async () => {
  if (!aiForm.product_name) { ElMessage.warning('Product name is required'); return }
  generating.value = true
  try {
    aiResult.value = await emailApi.generate(aiForm)
  } catch (e) {
    ElMessage.error('Failed to generate email')
  } finally {
    generating.value = false
  }
}

const useAiResult = () => {
  sendForm.subject = aiResult.value.subject
  sendForm.body = aiResult.value.body
  sendForm.template_id = null
  ElMessage.success('Email content applied')
}

const handleGenerateAI = () => {
  // Scroll to AI section
  document.querySelector('.el-card')?.scrollIntoView({ behavior: 'smooth' })
}

onMounted(loadData)
</script>