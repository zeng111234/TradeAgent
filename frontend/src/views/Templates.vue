<template>
  <div>
    <div class="page-header">
      <h2>{{ t('templates.title') }}</h2>
      <p>{{ t('templates.subtitle') }}</p>
    </div>

    <!-- Toolbar -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <el-row :gutter="16" align="middle">
        <el-col :span="8">
          <el-select v-model="filterCategory" :placeholder="t('templates.filterCategory')" clearable>
            <el-option label="Cold Outreach" value="cold_outreach" />
            <el-option label="Follow Up" value="follow_up" />
            <el-option label="Quotation" value="quotation" />
            <el-option label="Holiday" value="holiday" />
          </el-select>
        </el-col>
        <el-col :span="8">
          <el-select v-model="filterLanguage" :placeholder="t('templates.filterLanguage')" clearable>
            <el-option label="English" value="en" />
            <el-option label="Chinese" value="zh" />
            <el-option label="Spanish" value="es" />
          </el-select>
        </el-col>
        <el-col :span="8" style="text-align: right">
          <el-button type="primary" @click="showDialog()">
            <el-icon><Plus /></el-icon> {{ t('templates.newTemplate') }}
          </el-button>
        </el-col>
      </el-row>
    </el-card>

    <!-- Template List -->
    <el-row :gutter="16">
      <el-col :span="8" v-for="tmpl in templates" :key="tmpl.id" style="margin-bottom: 16px">
        <el-card shadow="hover">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600; font-size: 14px">{{ tmpl.name }}</span>
              <div>
                <el-tag v-if="tmpl.category" size="small" style="margin-right: 4px">{{ templateCategoryLabel(tmpl.category) }}</el-tag>
                <el-tag v-if="tmpl.is_ai_generated" type="success" size="small">AI</el-tag>
              </div>
            </div>
          </template>
          <p style="color: var(--text-3); font-size: 13px; margin-bottom: 8px">
            <strong>{{ t('templates.subjectLabel') }}:</strong>&nbsp;{{ tmpl.subject }}
          </p>
          <div style="color: var(--text-2); font-size: 13px; max-height: 80px; overflow: hidden" v-html="tmpl.body"></div>
          <div style="margin-top: 12px; display: flex; justify-content: space-between; align-items: center">
            <span style="color: var(--text-disabled); font-size: 12px">{{ t('templates.usedTimes', { n: tmpl.use_count }) }}</span>
            <div>
              <el-button size="small" type="primary" link @click="showDialog(tmpl)">{{ t('common.edit') }}</el-button>
              <el-button size="small" type="danger" link @click="handleDelete(tmpl)">{{ t('common.delete') }}</el-button>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="24" v-if="!templates.length">
        <el-empty :description="t('templates.empty')" />
      </el-col>
    </el-row>

    <!-- Edit Dialog -->
    <el-dialog v-model="dialogVisible" :title="editing ? t('templates.editTemplate') : t('templates.newTemplateTitle')" width="700px">
      <el-form :model="form" label-width="120px">
        <el-form-item :label="t('common.name')" required>
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item label="Category">
          <el-select v-model="form.category" style="width: 100%" clearable>
            <el-option label="Cold Outreach" value="cold_outreach" />
            <el-option label="Follow Up" value="follow_up" />
            <el-option label="Quotation" value="quotation" />
            <el-option label="Holiday" value="holiday" />
          </el-select>
        </el-form-item>
        <el-form-item label="Language">
          <el-select v-model="form.language" style="width: 100%">
            <el-option label="English" value="en" />
            <el-option label="Chinese" value="zh" />
            <el-option label="Spanish" value="es" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('templates.subjectLabel')" required>
          <el-input v-model="form.subject" placeholder="Use {variable_name} for variables" />
        </el-form-item>
        <el-form-item label="Body" required>
          <el-input v-model="form.body" type="textarea" :rows="10" placeholder="HTML supported. Use {variable_name} for variables like {company_name}, {contact_name}, {product_name}" />
        </el-form-item>
        <el-form-item>
          <el-alert type="info" :closable="false" show-icon>
            <template #title>
              Available variables: {company_name}, {contact_name}, {contact_title}, {product_name}, {country}, {industry}
            </template>
          </el-alert>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">Cancel</el-button>
        <el-button type="primary" @click="handleSave" :loading="saving">Save</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { emailApi } from '../api'
import { templateCategoryLabel } from '../constants/status.js'
import { t } from '../i18n/index.js'

const templates = ref([])
const dialogVisible = ref(false)
const editing = ref(null)
const saving = ref(false)
const filterCategory = ref('')
const filterLanguage = ref('')

const form = reactive({
  name: '',
  subject: '',
  body: '',
  language: 'en',
  category: '',
})

const loadTemplates = async () => {
  try {
    templates.value = await emailApi.getTemplates({
      category: filterCategory.value || undefined,
      language: filterLanguage.value || undefined,
    })
  } catch (e) {
    ElMessage.error('Failed to load templates')
  }
}

watch([filterCategory, filterLanguage], loadTemplates)

const showDialog = (tmpl) => {
  if (tmpl) {
    editing.value = tmpl
    Object.assign(form, { name: tmpl.name, subject: tmpl.subject, body: tmpl.body, language: tmpl.language, category: tmpl.category || '' })
  } else {
    editing.value = null
    Object.assign(form, { name: '', subject: '', body: '', language: 'en', category: '' })
  }
  dialogVisible.value = true
}

const handleSave = async () => {
  if (!form.name || !form.subject || !form.body) {
    ElMessage.warning('Name, subject and body are required')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await emailApi.updateTemplate(editing.value.id, form)
      ElMessage.success('Template updated')
    } else {
      await emailApi.createTemplate(form)
      ElMessage.success('Template created')
    }
    dialogVisible.value = false
    loadTemplates()
  } catch (e) {
    ElMessage.error('Failed to save template')
  } finally {
    saving.value = false
  }
}

const handleDelete = async (tmpl) => {
  try {
    await ElMessageBox.confirm(`Delete template "${tmpl.name}"?`, 'Confirm', { type: 'warning' })
    await emailApi.deleteTemplate(tmpl.id)
    ElMessage.success('Template deleted')
    loadTemplates()
  } catch (e) { /* cancelled */ }
}

onMounted(loadTemplates)
</script>