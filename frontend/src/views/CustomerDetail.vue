<template>
  <div v-loading="loading">
    <div class="page-header">
      <el-button @click="$router.push('/customers')" style="margin-bottom: 12px">
        <el-icon><ArrowLeft /></el-icon> {{ t('customerDetail.back') }}
      </el-button>
      <h2>{{ customer.company_name }}</h2>
      <p>{{ customer.country }} {{ customer.city ? '- ' + customer.city : '' }} | {{ customer.industry || 'N/A' }}</p>
    </div>

    <el-row :gutter="20">
      <!-- Left: Customer Info -->
      <el-col :span="16">
        <!-- Info Card -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">{{ t('customerDetail.info') }}</span>
              <el-tag :type="stageTagType(customer.stage)">{{ stageLabel(customer.stage) }}</el-tag>
            </div>
          </template>
          <el-descriptions :column="2" border>
            <el-descriptions-item :label="t('common.company')">{{ customer.company_name }}</el-descriptions-item>
            <el-descriptions-item :label="t('customerDetail.companyCn')">{{ customer.company_name_cn || '-' }}</el-descriptions-item>
            <el-descriptions-item :label="t('common.country')">{{ customer.country || '-' }}</el-descriptions-item>
            <el-descriptions-item :label="t('customerDetail.city')">{{ customer.city || '-' }}</el-descriptions-item>
            <el-descriptions-item :label="t('dashboard.colWebsite')">
              <a v-if="customer.website" :href="customer.website" target="_blank" style="color: var(--accent)">{{ customer.website }}</a>
              <span v-else>-</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('common.industry')">{{ customer.industry || '-' }}</el-descriptions-item>
            <el-descriptions-item :label="t('customerDetail.products')" :span="2">{{ customer.products || '-' }}</el-descriptions-item>
            <el-descriptions-item :label="t('common.score')">
              <!-- Same bands as the gauge beside it (scoreColor / scoreClass). -->
              <span class="num score-text" :class="scoreClass(customer.score)">
                {{ customer.score }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('common.source')">{{ customer.source }}</el-descriptions-item>
            <el-descriptions-item :label="t('customerDetail.tags')">{{ customer.tags || '-' }}</el-descriptions-item>
            <el-descriptions-item :label="t('customerDetail.annualImport')">{{ customer.annual_import_value || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>

        <!-- Contacts -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">{{ t('customerDetail.contacts') }}</span>
              <el-button size="small" type="primary" @click="showContactDialog = true">
                <el-icon><Plus /></el-icon> {{ t('customerDetail.addContact') }}
              </el-button>
            </div>
          </template>
          <el-table :data="customer.contacts || []" stripe>
            <el-table-column prop="name" :label="t('customerDetail.colName')" min-width="120" show-overflow-tooltip />
            <el-table-column prop="title" :label="t('customerDetail.colTitle')" min-width="110" show-overflow-tooltip />
            <el-table-column prop="email" :label="t('customerDetail.colEmail')" min-width="180" show-overflow-tooltip />
            <el-table-column prop="phone" :label="t('customerDetail.colPhone')" min-width="120" show-overflow-tooltip />
            <el-table-column prop="whatsapp" :label="t('customerDetail.colWhatsapp')" min-width="110" show-overflow-tooltip />
            <el-table-column :label="t('customerDetail.colPrimary')" width="80" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.is_primary" type="success" size="small">{{ t('common.yes') }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- Notes Timeline -->
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">{{ t('customerDetail.timeline') }}</span>
              <el-button size="small" type="primary" @click="showNoteDialog = true">
                <el-icon><Plus /></el-icon> {{ t('customerDetail.addNote') }}
              </el-button>
            </div>
          </template>
          <el-timeline v-if="customer.notes_list && customer.notes_list.length">
            <el-timeline-item
              v-for="note in customer.notes_list"
              :key="note.id"
              :timestamp="formatDate(note.created_at)"
              placement="top"
              :type="noteTypeColor(note.note_type)"
            >
              <el-card shadow="never" body-style="padding: 12px">
                <el-tag size="small" :type="noteTypeColor(note.note_type)" style="margin-bottom: 4px">{{ noteTypeLabel(note.note_type) }}</el-tag>
                <p style="margin: 0; color: var(--text-2)">{{ note.content }}</p>
              </el-card>
            </el-timeline-item>
          </el-timeline>
          <el-empty v-else :description="t('customerDetail.noNotes')" />
        </el-card>
      </el-col>

      <!-- Right: Quick Actions -->
      <el-col :span="8">
        <!-- Stage Update -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header><span style="font-weight: 600">{{ t('customerDetail.updateStage') }}</span></template>
          <el-select v-model="customer.stage" style="width: 100%; margin-bottom: 12px" @change="updateStage">
            <el-option :label="t('status.stage.new')" value="new" />
            <el-option :label="t('status.stage.contacted')" value="contacted" />
            <el-option :label="t('status.stage.interested')" value="interested" />
            <el-option :label="t('status.stage.quoting')" value="quoting" />
            <el-option :label="t('status.stage.sample')" value="sample" />
            <el-option :label="t('status.stage.ordering')" value="ordering" />
            <el-option :label="t('status.stage.completed')" value="completed" />
            <el-option :label="t('status.stage.lost')" value="lost" />
          </el-select>
        </el-card>

        <!-- AI Follow-up Suggestion -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">{{ t('customerDetail.aiAdvice') }}</span>
              <el-button size="small" type="primary" @click="getFollowUpAdvice" :loading="followUpLoading">
                <el-icon><Aim /></el-icon>
              </el-button>
            </div>
          </template>
          <div v-if="followUpAdvice">
            <el-alert :title="followUpAdvice.urgency" :type="followUpAdvice.urgency_type" :closable="false" show-icon style="margin-bottom: 12px" />
            <p style="margin: 0 0 8px; font-size: 13px; color: var(--text-1)">{{ followUpAdvice.advice }}</p>
            <div v-if="followUpAdvice.next_steps && followUpAdvice.next_steps.length">
              <p style="margin: 0 0 4px; font-size: 12px; color: var(--text-3); font-weight: 600">{{ t('customerDetail.nextSteps') }}:</p>
              <ul style="margin: 0; padding-left: 16px">
                <li v-for="s in followUpAdvice.next_steps" :key="s" style="font-size: 12px; color: var(--text-2); margin-bottom: 2px">{{ s }}</li>
              </ul>
            </div>
            <el-button v-if="followUpAdvice.draft_email" size="small" style="margin-top: 8px" @click="showFollowUpDraft = !showFollowUpDraft">
              {{ showFollowUpDraft ? t('customerDetail.hideDraft') : t('customerDetail.showDraft') }}
            </el-button>
            <div v-if="showFollowUpDraft && followUpAdvice.draft_email" style="background: var(--bg-subtle); padding: 12px; border-radius: 6px; margin-top: 8px">
              <div v-html="followUpAdvice.draft_email" style="font-size: 13px; color: var(--text-1)"></div>
              <el-button size="small" type="primary" style="margin-top: 8px" @click="sendFollowUpDraft">{{ t('customerDetail.sendThisEmail') }}</el-button>
            </div>
          </div>
          <el-empty v-else :description="t('customerDetail.adviceEmpty')" :image-size="40" />
        </el-card>

        <!-- Quick Email -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header><span style="font-weight: 600">{{ t('customerDetail.quickActions') }}</span></template>
          <el-button type="primary" style="width: 100%; margin-bottom: 8px" @click="$router.push('/emails')">
            <el-icon><Message /></el-icon> {{ t('customerDetail.sendEmail') }}
          </el-button>
          <el-button style="width: 100%; margin-bottom: 8px" @click="$router.push('/tasks')">
            <el-icon><Calendar /></el-icon> {{ t('customerDetail.createTask') }}
          </el-button>
          <el-button style="width: 100%; margin-bottom: 8px" @click="$router.push('/agent')">
            <el-icon><Tools /></el-icon> {{ t('customerDetail.aiAgent') }}
          </el-button>
        </el-card>

        <!-- AI Score -->
        <el-card shadow="never">
          <template #header><span style="font-weight: 600">{{ t('customerDetail.aiScore') }}</span></template>
          <div style="text-align: center; padding: 20px 0">
            <el-progress type="dashboard" :percentage="customer.score" :color="scoreColor" :width="120" />
            <p style="margin-top: 12px; color: var(--text-3); font-size: 13px">{{ t('customerDetail.aiScoreHint') }}</p>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Add Contact Dialog -->
    <el-dialog v-model="showContactDialog" title="Add Contact" width="500px">
      <el-form :model="contactForm" label-width="100px">
        <el-form-item :label="t('customerDetail.colName')" required>
          <el-input v-model="contactForm.name" />
        </el-form-item>
        <el-form-item :label="t('customerDetail.colTitle')">
          <el-input v-model="contactForm.title" />
        </el-form-item>
        <el-form-item label="Email">
          <el-input v-model="contactForm.email" />
        </el-form-item>
        <el-form-item label="Phone">
          <el-input v-model="contactForm.phone" />
        </el-form-item>
        <el-form-item label="WhatsApp">
          <el-input v-model="contactForm.whatsapp" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showContactDialog = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" @click="addContact">Save</el-button>
      </template>
    </el-dialog>

    <!-- Add Note Dialog -->
    <el-dialog v-model="showNoteDialog" title="Add Note" width="500px">
      <el-form :model="noteForm" label-width="100px">
        <el-form-item label="Type">
          <el-select v-model="noteForm.note_type" style="width: 100%">
            <el-option label="General" value="general" />
            <el-option label="Email" value="email" />
            <el-option label="Call" value="call" />
            <el-option label="Meeting" value="meeting" />
            <el-option label="Quotation" value="quotation" />
          </el-select>
        </el-form-item>
        <el-form-item label="Content" required>
          <el-input v-model="noteForm.content" type="textarea" :rows="4" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showNoteDialog = false">Cancel</el-button>
        <el-button type="primary" @click="addNote">Save</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { customerApi } from '../api'
import { noteTypeColor, noteTypeLabel, scoreClass, scoreColor, stageLabel, stageTagType } from '../constants/status.js'
import { t } from '../i18n/index.js'

const route = useRoute()
const loading = ref(false)
const customer = ref({})
const showContactDialog = ref(false)
const showNoteDialog = ref(false)

const contactForm = reactive({ name: '', title: '', email: '', phone: '', whatsapp: '' })
const noteForm = reactive({ content: '', note_type: 'general' })

const formatDate = (d) => {
  if (!d) return ''
  return new Date(d).toLocaleString()
}

const loadCustomer = async () => {
  loading.value = true
  try {
    customer.value = await customerApi.get(route.params.id)
  } catch (e) {
    ElMessage.error('Failed to load customer')
  } finally {
    loading.value = false
  }
}

const updateStage = async (stage) => {
  try {
    // Same concurrency guard as the edit dialog: refuse rather than overwrite
    // a change someone else saved while this page was open.
    await customerApi.update(route.params.id, {
      stage,
      expected_updated_at: customer.value?.updated_at,
    })
    ElMessage.success('Stage updated')
    loadCustomer()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || 'Failed to update stage')
    loadCustomer()
  }
}

const addContact = async () => {
  if (!contactForm.name.trim()) { ElMessage.warning('Name is required'); return }
  try {
    await customerApi.addContact(route.params.id, contactForm)
    ElMessage.success('Contact added')
    showContactDialog.value = false
    Object.assign(contactForm, { name: '', title: '', email: '', phone: '', whatsapp: '' })
    loadCustomer()
  } catch (e) {
    ElMessage.error('Failed to add contact')
  }
}

const addNote = async () => {
  if (!noteForm.content.trim()) { ElMessage.warning('Content is required'); return }
  try {
    await customerApi.addNote(route.params.id, noteForm)
    ElMessage.success('Note added')
    showNoteDialog.value = false
    noteForm.content = ''
    loadCustomer()
  } catch (e) {
    ElMessage.error('Failed to add note')
  }
}

// AI Follow-up
const followUpLoading = ref(false)
const followUpAdvice = ref(null)
const showFollowUpDraft = ref(false)

const getFollowUpAdvice = async () => {
  if (!customer.value || !customer.value.stage) return
  followUpLoading.value = true
  try {
    const { agentApi, emailApi } = await import('../api')

    // Build context from customer data
    const notes = (customer.value.notes_list || []).map(n => n.content).join('; ')
    const contacts = (customer.value.contacts || []).map(c => `${c.name} (${c.email || 'no email'})`).join(', ')

    // Stage-based advice
    const stage = customer.value.stage
    let urgency = ''
    let urgencyType = ''
    let advice = ''
    let nextSteps = []

    if (stage === 'new') {
      urgency = 'Not yet contacted'
      urgencyType = 'info'
      advice = `This customer hasn't been contacted yet. Send an initial outreach email introducing your products.`
      nextSteps = [
        'Send a personalized cold email',
        'Reference their industry/products to show relevance',
        'Include your catalog or key product photos',
      ]
    } else if (stage === 'contacted') {
      urgency = 'Waiting for response'
      urgencyType = 'warning'
      advice = `You've reached out but haven't heard back. Wait a few days, then try a different angle.`
      nextSteps = [
        'Wait 3-5 days before follow-up',
        'Try a different subject line or value proposition',
        'Consider reaching out via a different channel (LinkedIn, WhatsApp)',
      ]
    } else if (stage === 'interested') {
      urgency = 'High potential - keep momentum!'
      urgencyType = 'success'
      advice = `This customer showed interest. Send detailed product info and case studies to keep them engaged.`
      nextSteps = [
        'Send detailed product specifications',
        'Share success stories from similar customers',
        'Offer a sample or trial order',
      ]
    } else if (stage === 'quoting') {
      urgency = 'In negotiations - follow up soon!'
      urgencyType = 'danger'
      advice = `You're in the quoting stage. Follow up on the quote, address any concerns about price or terms.`
      nextSteps = [
        'Follow up on the quoted price within 2-3 days',
        'Ask if they need modifications to the quote',
        'Offer volume discount or payment term flexibility',
      ]
    } else if (stage === 'sample') {
      urgency = 'Sample stage - check progress'
      urgencyType = 'warning'
      advice = `Samples have been sent. Follow up on feedback and quality assessment.`
      nextSteps = [
        'Ask about sample receipt and quality feedback',
        'Offer technical support for testing',
        'Discuss MOQ and lead time for bulk order',
      ]
    } else {
      urgency = 'Stage: ' + stage
      urgencyType = 'info'
      advice = `Current stage: ${stage}. Manage accordingly.`
      nextSteps = ['Review customer status and take appropriate action']
    }

    // Generate draft email if customer has a contact
    const primaryContact = (customer.value.contacts || []).find(c => c.is_primary) || (customer.value.contacts || [])[0]
    if (primaryContact && primaryContact.email) {
      try {
        const emailResult = await agentApi.analyzeEmail({
          email_content: `Customer: ${customer.value.company_name}\nIndustry: ${customer.value.industry || 'N/A'}\nStage: ${stage}\nNotes: ${notes}\nContact: ${primaryContact.name}`,
        })
        if (emailResult.suggested_reply_points) {
          nextSteps = emailResult.suggested_reply_points
        }
      } catch (e) {
        // Use stage-based advice
      }
    }

    followUpAdvice.value = { urgency, urgency_type: urgencyType, advice, next_steps: nextSteps }
  } catch (e) {
    ElMessage.error('Failed to get advice')
  } finally {
    followUpLoading.value = false
  }
}

const sendFollowUpDraft = async () => {
  if (!followUpAdvice.value?.draft_email) return
  const primaryContact = (customer.value.contacts || []).find(c => c.is_primary) || (customer.value.contacts || [])[0]
  if (!primaryContact?.email) {
    ElMessage.warning('No contact email available')
    return
  }
  try {
    const { emailApi } = await import('../api')
    await emailApi.send({
      to_email: primaryContact.email,
      to_name: primaryContact.name,
      subject: `Following up - ${customer.value.company_name}`,
      body: followUpAdvice.value.draft_email,
      customer_id: customer.value.id,
    })
    ElMessage.success('Email sent!')
    showFollowUpDraft.value = false
  } catch (e) {
    ElMessage.error('Failed to send email')
  }
}

onMounted(loadCustomer)
</script>