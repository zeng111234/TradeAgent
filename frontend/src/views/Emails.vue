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
          <div>
            <el-button size="small" @click="loadData" :loading="false">
              <el-icon><Refresh /></el-icon> Refresh
            </el-button>
            <span v-if="batchProgress" style="margin-right: 8px; font-size: 12px; color: #909399">{{ batchProgress }}</span>
          <el-button type="success" size="small" @click="sendAllPending" :loading="sendingAll" :disabled="!pendingEmails.length">
              <el-icon><Promotion /></el-icon> Send All ({{ pendingEmails.length }})
            </el-button>
          </div>
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
            <el-button type="danger" size="small" @click="handleRejectPending(email)" plain>
              <el-icon><CircleClose /></el-icon> Reject
            </el-button>
            <el-button size="small" @click="handleDeletePending(email)" plain>
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
            <h4 style="margin: 0 0 8px">
              Generated Email:
              <el-tag v-if="aiResult.ai_powered" type="success" size="small">AI 生成</el-tag>
              <el-tag v-else type="warning" size="small">模板（非 AI）</el-tag>
            </h4>
            <el-alert
              v-if="!aiResult.ai_powered && aiResult.fallback_reason"
              type="warning"
              :closable="false"
              show-icon
              style="margin-bottom: 8px"
              :title="`以下内容是内置模板，不是模型写的。原因：${aiResult.fallback_reason}`"
            />
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
            <el-table-column label="Reply" width="130">
              <template #default="{ row }">
                <span v-if="row.status === 'replied'" style="color: #909399; font-size: 12px">
                  {{ formatDate(row.replied_at) }}
                </span>
                <el-button
                  v-else-if="row.status === 'sent' || row.status === 'opened' || row.status === 'delivered'"
                  size="small"
                  @click="markReplied(row)"
                >
                  Mark replied
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
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
const sendingAll = ref(false)
const batchProgress = ref('')

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
    const result = await emailApi.send(sendForm)
    // A repeated send comes back as the first one's record instead of a second
    // email. Saying "sent" there would be true but misleading -- the user
    // clicked twice and should know only one went out.
    if (result?.replayed) {
      ElMessage.info('That email was already sent a moment ago - not sent again.')
    } else {
      ElMessage.success('Email sent successfully!')
    }
    sendForm.to_email = ''
    sendForm.to_name = ''
    loadData()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || 'Failed to send email')
  } finally {
    sending.value = false
  }
}

const markReplied = async (row) => {
  // When they wrote, not when we noticed.
  //
  // The endpoint has always accepted `replied_at`, and the whole conversation
  // derivation hangs off it: a reply recorded as "now" when it actually arrived
  // three months ago makes the plan read a cold thread as a live one -- "they
  // replied 0 day(s) ago", priority high, and a routine answer proposed for a
  // conversation that ended a season ago. The common case is a reply that just
  // arrived, so that is one click; the rare case now has somewhere to go.
  let repliedAt = null
  try {
    await ElMessageBox.confirm(
      'Did this reply arrive just now?',
      'Record a reply',
      {
        confirmButtonText: 'Just now',
        cancelButtonText: 'They wrote earlier',
        type: 'info',
        distinguishCancelAndClose: true,
      }
    )
  } catch (action) {
    if (action !== 'cancel') return // dialog closed: record nothing
    let value
    try {
      ({ value } = await ElMessageBox.prompt(
        'When did they write? (YYYY-MM-DD)',
        'Record an earlier reply',
        {
          inputPattern: /^\d{4}-\d{2}-\d{2}$/,
          inputErrorMessage: 'Use YYYY-MM-DD',
          inputPlaceholder: '2025-08-15',
        }
      ))
    } catch (cancelled) {
      return
    }
    // Midnight, so a reply dated today is never "in the future" for a server
    // running on UTC.
    repliedAt = `${value}T00:00:00`
  }

  try {
    const result = await emailApi.recordReply(row.id, repliedAt ? { replied_at: repliedAt } : {})
    const conversation = result.conversation || {}
    if (result.already_recorded) {
      ElMessage.info('This reply was already recorded')
    } else if (conversation.they_wrote_last) {
      const when = repliedAt ? ` (recorded as ${repliedAt.slice(0, 10)})` : ''
      ElMessage.success(`Reply recorded${when} - you owe them an answer`)
    } else {
      ElMessage.success('Reply recorded')
    }
    loadData()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || 'Could not record the reply')
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

const saveEdit = async (email) => {
  // Save to backend
  try {
    await emailApi.updateLog(email.id, {
      subject: editSubject.value,
      body: editBody.value,
    })
    email.subject = editSubject.value
    email.body = editBody.value
    editingId.value = null
    ElMessage.success('Draft updated. Click Send to send it.')
  } catch (e) {
    ElMessage.error('Failed to save: ' + (e.response?.data?.detail || e.message))
  }
}

const handleSendPending = async (email) => {
  sendingId.value = email.id
  try {
    await emailApi.sendSinglePending(email.id)
    ElMessage.success(`Email sent to ${email.to_email}!`)
    // Refresh lists
    await loadData()
  } catch (e) {
    ElMessage.error('Failed to send: ' + (e.response?.data?.detail || e.message))
  } finally {
    sendingId.value = null
  }
}

const sendAllPending = async () => {
  // Drain in bounded batches, showing the queue shrink.
  //
  // Sending the whole queue in one request was one call that could run for
  // minutes, hold the send lock throughout, and time out client-side with the
  // operator none the wiser about how much went out. The server now takes a
  // limit and reports what is left, so the loop belongs here -- and if it does
  // stop early (a failure, a closed tab), the rows already sent stay sent and
  // the next run continues from where this one stopped.
  const BATCH = 25
  const MAX_ROUNDS = 40
  let sent = 0
  let failed = 0
  let remaining = pendingEmails.value.length

  sendingAll.value = true
  try {
    for (let round = 0; round < MAX_ROUNDS; round += 1) {
      const result = await emailApi.sendPending(BATCH)
      sent += result.sent || 0
      failed += result.failed || 0
      remaining = result.remaining || 0
      batchProgress.value = `Sent ${sent}${failed ? `, ${failed} failed` : ''} - ${remaining} left`
      if (!result.total || remaining === 0) break
    }

    if (sent > 0) {
      ElMessage.success(`Sent ${sent} emails.${failed > 0 ? ` ${failed} failed.` : ''}`)
    } else {
      ElMessage.info(`No emails sent.${failed > 0 ? ` ${failed} failed.` : ''}`)
    }
    if (remaining > 0) {
      ElMessage.warning(`${remaining} still pending - send again to continue.`)
    }
    await loadData()
  } catch (e) {
    ElMessage.error(
      `Stopped after ${sent} sent${failed ? `, ${failed} failed` : ''}: ` +
      (e.response?.data?.detail || e.message)
    )
    await loadData()
  } finally {
    sendingAll.value = false
    batchProgress.value = ''
  }
}

const handleDeletePending = async (email) => {
  try {
    await emailApi.deleteLog(email.id)
    pendingEmails.value = pendingEmails.value.filter(e => e.id !== email.id)
    ElMessage.success('Draft deleted.')
  } catch (e) {
    ElMessage.error('Failed to delete draft')
  }
}

// Rejecting is a decision, not housekeeping: the row is kept so the agent's next
// run can see that this approach was already turned down. Deleting is still
// there for a draft you wrote yourself and simply do not want.
const handleRejectPending = async (email) => {
  try {
    await ElMessageBox.confirm(
      'Reject this draft? It is kept as a rejected proposal, so the next plan ' +
      'knows this approach was turned down. It will not be sent.',
      'Reject draft',
      { type: 'warning', confirmButtonText: 'Reject', cancelButtonText: 'Cancel' },
    )
  } catch (e) {
    return                              // the human cancelled
  }
  try {
    const result = await emailApi.rejectDraft(email.id)
    if (result.already_rejected) {
      ElMessage.info('This draft was already rejected')
    } else {
      ElMessage.success('Rejected and recorded')
    }
    loadData()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || 'Could not reject the draft')
  }
}

onMounted(loadData)
</script>