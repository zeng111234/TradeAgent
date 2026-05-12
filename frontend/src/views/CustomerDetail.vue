<template>
  <div v-loading="loading">
    <div class="page-header">
      <el-button @click="$router.push('/customers')" style="margin-bottom: 12px">
        <el-icon><ArrowLeft /></el-icon> Back to Customers
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
              <span style="font-weight: 600">Customer Information</span>
              <el-tag :type="stageTagType(customer.stage)">{{ stageLabel(customer.stage) }}</el-tag>
            </div>
          </template>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="Company">{{ customer.company_name }}</el-descriptions-item>
            <el-descriptions-item label="Chinese Name">{{ customer.company_name_cn || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Country">{{ customer.country || '-' }}</el-descriptions-item>
            <el-descriptions-item label="City">{{ customer.city || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Website">
              <a v-if="customer.website" :href="customer.website" target="_blank" style="color: #409eff">{{ customer.website }}</a>
              <span v-else>-</span>
            </el-descriptions-item>
            <el-descriptions-item label="Industry">{{ customer.industry || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Products" :span="2">{{ customer.products || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Score">
              <span :style="{ color: customer.score >= 70 ? '#67c23a' : '#e6a23c', fontWeight: 700, fontSize: '18px' }">
                {{ customer.score }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="Source">{{ customer.source }}</el-descriptions-item>
            <el-descriptions-item label="Tags">{{ customer.tags || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Annual Import">{{ customer.annual_import_value || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>

        <!-- Contacts -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">Contacts</span>
              <el-button size="small" type="primary" @click="showContactDialog = true">
                <el-icon><Plus /></el-icon> Add Contact
              </el-button>
            </div>
          </template>
          <el-table :data="customer.contacts || []" stripe>
            <el-table-column prop="name" label="Name" />
            <el-table-column prop="title" label="Title" />
            <el-table-column prop="email" label="Email" />
            <el-table-column prop="phone" label="Phone" />
            <el-table-column prop="whatsapp" label="WhatsApp" />
            <el-table-column label="Primary" width="80" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.is_primary" type="success" size="small">Yes</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <!-- Notes Timeline -->
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">Communication Timeline</span>
              <el-button size="small" type="primary" @click="showNoteDialog = true">
                <el-icon><Plus /></el-icon> Add Note
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
                <el-tag size="small" :type="noteTypeColor(note.note_type)" style="margin-bottom: 4px">{{ note.note_type }}</el-tag>
                <p style="margin: 0; color: #606266">{{ note.content }}</p>
              </el-card>
            </el-timeline-item>
          </el-timeline>
          <el-empty v-else description="No notes yet" />
        </el-card>
      </el-col>

      <!-- Right: Quick Actions -->
      <el-col :span="8">
        <!-- Stage Update -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header><span style="font-weight: 600">Update Stage</span></template>
          <el-select v-model="customer.stage" style="width: 100%; margin-bottom: 12px" @change="updateStage">
            <el-option label="New" value="new" />
            <el-option label="Contacted" value="contacted" />
            <el-option label="Interested" value="interested" />
            <el-option label="Quoting" value="quoting" />
            <el-option label="Sample" value="sample" />
            <el-option label="Ordering" value="ordering" />
            <el-option label="Completed" value="completed" />
            <el-option label="Lost" value="lost" />
          </el-select>
        </el-card>

        <!-- Quick Email -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <template #header><span style="font-weight: 600">Quick Actions</span></template>
          <el-button type="primary" style="width: 100%; margin-bottom: 8px" @click="$router.push('/emails')">
            <el-icon><Message /></el-icon> Send Email
          </el-button>
          <el-button style="width: 100%; margin-bottom: 8px" @click="$router.push('/tasks')">
            <el-icon><Calendar /></el-icon> Create Task
          </el-button>
        </el-card>

        <!-- AI Score -->
        <el-card shadow="never">
          <template #header><span style="font-weight: 600">AI Match Score</span></template>
          <div style="text-align: center; padding: 20px 0">
            <el-progress type="dashboard" :percentage="customer.score" :color="scoreColor" :width="120" />
            <p style="margin-top: 12px; color: #909399; font-size: 13px">Product match score</p>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- Add Contact Dialog -->
    <el-dialog v-model="showContactDialog" title="Add Contact" width="500px">
      <el-form :model="contactForm" label-width="100px">
        <el-form-item label="Name" required>
          <el-input v-model="contactForm.name" />
        </el-form-item>
        <el-form-item label="Title">
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
        <el-button @click="showContactDialog = false">Cancel</el-button>
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

const route = useRoute()
const loading = ref(false)
const customer = ref({})
const showContactDialog = ref(false)
const showNoteDialog = ref(false)

const contactForm = reactive({ name: '', title: '', email: '', phone: '', whatsapp: '' })
const noteForm = reactive({ content: '', note_type: 'general' })

const scoreColor = [
  { color: '#f56c6c', percentage: 30 },
  { color: '#e6a23c', percentage: 60 },
  { color: '#67c23a', percentage: 100 },
]

const stageLabel = (stage) => {
  const map = { new: 'New', contacted: 'Contacted', interested: 'Interested', quoting: 'Quoting', sample: 'Sample', ordering: 'Ordering', completed: 'Completed', lost: 'Lost' }
  return map[stage] || stage
}

const stageTagType = (stage) => {
  const map = { new: 'info', contacted: '', interested: 'success', quoting: 'warning', sample: 'warning', ordering: 'warning', completed: 'success', lost: 'danger' }
  return map[stage] || ''
}

const noteTypeColor = (type) => {
  const map = { general: '', email: 'primary', call: 'success', meeting: 'warning', quotation: 'danger' }
  return map[type] || ''
}

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
    await customerApi.update(route.params.id, { stage })
    ElMessage.success('Stage updated')
  } catch (e) {
    ElMessage.error('Failed to update stage')
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

onMounted(loadCustomer)
</script>