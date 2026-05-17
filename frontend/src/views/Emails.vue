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

    <!-- Pending Draft Review -->
    <el-card shadow="never" style="margin-bottom: 20px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <div>
            <span style="font-weight: 600">Pending Draft Review</span>
            <el-tag v-if="pendingEmails.length" type="warning" size="small" style="margin-left: 8px">{{ pendingEmails.length }} drafts</el-tag>
          </div>
          <el-button size="small" @click="loadData" :loading="false">
            <el-icon><Refresh /></el-icon> Refresh
          </el-button>
        </div>
      </template>

      <el-empty v-if="!pendingEmails.length" description="No pending drafts. Import leads from Dashboard first." />

      <div v-for="email in pendingEmails" :key="email.id" style="border: 1px solid #ebeef5; border-radius: 8px; padding: 16px; margin-bottom: 12px; background: #fafafa">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px">
          <div>
            <strong>To:</strong> {{ email.to_email }}
            <span v-if="email.to_name" style="color: #909399; margin-left: 8px">({{ email.to_name }})</span>
          </div>
          <div>
            <el-tag type="warning" size="small">Pending</el-tag>
          </div>
        </div>
        <div style="margin-bottom: 8px">
          <strong>Subject:</strong>
          <span v-if="editingId !== email.id">{{ email.subject }}</span>
          <el-input v-else v-model="editSubject" size="small" style="width: 80%; margin-left: 4px" />
        </div>
        <div style="margin-bottom: 12px; color: #606266; font-size: 13px; line-height: 1.6">
          <div v-if="editingId !== email.id" style="white-space: pre-wrap; background: #fff; padding: 12px; border-radius: 4px; border: 1px solid #eee">{{ email.body }}</div>
          <el-input v-else v-model="editBody" type="textarea" :rows="5" />
        </div>
        <div style="display: flex; gap: 8px">
          <template v-if="editingId !== email.id">
            <el-button type="primary" size="small" @click="handleSendPending(email)" :loading="sendingId === email.id">
              <el-icon><Promotion /></el-icon> Send
            </el-button>
            <el-button size="small" @click="startEdit(email)">
              <el-icon><Edit /></el-icon> Edit
            </el-button>
            <el-button type="danger" size="small" @click="handleDeletePending(email)" plain>
              <el-icon><Delete /></el-icon> Delete
            </el-button>
          </template>
          <template v-else>
            <el-button type="success" size="small" @click="saveEdit(email)">
              <el-icon><Check /></el-icon> Save
            </el-button>
            <el-button size="small" @click="cancelEdit">Cancel</el-button>
          </template>
        </div>
      </div>
    </el-card>

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

// Pending drafts from daily scan import
const pendingEmails = ref([])
const editingId = ref(null)
const editSubject = ref('')
const editBody = ref('')
const sendingId = ref(null)

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
    // Filter pending drafts (from daily scan import)
    pendingEmails.value = logs.filter(l => l.status === 'pending')
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

// Pending draft actions
const startEdit = (email) => {
  editingId.value = email.id
  editSubject.value = email.subject
  editBody.value = email.body
}

const cancelEdit = () => {
  editingId.value = null
  editSubject.value = ''
  editBody.value = ''
}

const saveEdit = (email) => {
  // Update local data
  email.subject = editSubject.value
  email.body = editBody.value
  editingId.value = null
  ElMessage.success('Draft updated locally. Click Send to send it.')
}

const handleSendPending = async (email) => {
  sendingId.value = email.id
  try {
    await emailApi.send({
      to_email: email.to_email,
      to_name: email.to_name || '',
      subject: email.subject,
      body: email.body,
    })
    ElMessage.success(`Email sent to ${email.to_email}!`)
    // Remove from pending list and refresh
    await loadData()
  } catch (e) {
    ElMessage.error('Failed to send: ' + (e.response?.data?.detail || e.message))
  } finally {
    sendingId.value = null
  }
}

const handleDeletePending = async (email) => {
  try {
    // Remove from local list (the backend will keep the log as 'failed' or we just filter it out)
    pendingEmails.value = pendingEmails.value.filter(e => e.id !== email.id)
    ElMessage.success('Draft removed from review list.')
  } catch (e) {
    ElMessage.error('Failed to remove draft')
  }
}

onMounted(loadData)
</script>