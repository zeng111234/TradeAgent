<template>
  <div>
    <div class="page-header">
      <h2>{{ t('senders.title') }}</h2>
      <p>{{ t('senders.subtitle') }}</p>
    </div>

    <!-- What a send would use right now. First, because the question the
         page exists to answer is "which mailbox am I about to send from". -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div class="effective">
        <span class="effective-label">{{ t('senders.effectiveLabel') }}</span>
        <el-tag v-if="status.effective === 'account'" type="success" effect="dark" size="large">
          {{ activeLabel }}
        </el-tag>
        <el-tag v-else-if="status.effective === 'env'" type="warning" size="large">
          {{ t('senders.effectiveEnv') }}
        </el-tag>
        <el-tag v-else type="danger" size="large">{{ t('senders.effectiveSimulated') }}</el-tag>
      </div>
      <p class="effective-hint">{{ statusHint }}</p>
    </el-card>

    <!-- Account list -->
    <el-card shadow="never">
      <div style="margin-bottom: 16px; text-align: right">
        <el-button type="primary" @click="showDialog()">
          <el-icon><Plus /></el-icon> {{ t('senders.new') }}
        </el-button>
      </div>

      <el-table :data="accounts" v-loading="loading" style="width: 100%">
        <el-table-column :label="t('senders.name')" prop="label" min-width="140" />
        <el-table-column :label="t('senders.address')" prop="username" min-width="220" />
        <el-table-column :label="t('senders.server')" min-width="220">
          <template #default="{ row }">
            <span style="font-family: monospace">{{ row.host }}:{{ row.port }}</span>
            <el-tag v-if="row.use_tls" size="small" style="margin-left: 8px">TLS</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('common.status')" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success" size="small">{{ t('senders.active') }}</el-tag>
            <span v-else style="color: var(--text-disabled)">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="220">
          <template #default="{ row }">
            <el-button
              size="small"
              type="primary"
              link
              :disabled="row.is_active"
              @click="handleActivate(row)"
            >{{ t('senders.activate') }}</el-button>
            <el-button size="small" type="primary" link @click="showDialog(row)">
              {{ t('common.edit') }}
            </el-button>
            <el-button size="small" type="danger" link @click="handleDelete(row)">
              {{ t('common.delete') }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="t('senders.empty')" />
        </template>
      </el-table>
    </el-card>

    <!-- Create / edit dialog -->
    <el-dialog
      v-model="dialogVisible"
      :title="editing ? t('senders.edit') : t('senders.newTitle')"
      width="560px"
    >
      <el-form :model="form" label-width="110px">
        <el-form-item :label="t('senders.name')" required>
          <el-input v-model="form.label" :placeholder="t('senders.namePlaceholder')" maxlength="80" />
        </el-form-item>
        <el-form-item :label="t('senders.address')" required>
          <el-input v-model="form.username" placeholder="name@qq.com" maxlength="254" />
        </el-form-item>
        <el-form-item :label="t('senders.password')" required>
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="editing ? t('senders.passwordKeep') : t('senders.passwordPlaceholder')"
            maxlength="200"
          />
        </el-form-item>
        <el-form-item>
          <el-alert type="info" :closable="false" show-icon>
            <template #title>{{ t('senders.passwordHint') }}</template>
          </el-alert>
        </el-form-item>
        <el-form-item :label="t('senders.server')" required>
          <el-row :gutter="12">
            <el-col :span="16">
              <el-input v-model="form.host" placeholder="smtp.qq.com" maxlength="120" />
            </el-col>
            <el-col :span="8">
              <el-input v-model.number="form.port" type="number" />
            </el-col>
          </el-row>
        </el-form-item>
        <el-form-item :label="t('senders.useTls')">
          <el-switch v-model="form.use_tls" />
        </el-form-item>
        <el-form-item v-if="!editing" :label="t('senders.activateNow')">
          <el-switch v-model="form.activate" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">
          {{ t('common.save') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, h, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { sendersApi } from '../api'
import { t } from '../i18n/index.js'

const accounts = ref([])
const status = ref({ effective: 'simulated', active_account_id: null, env_configured: false })
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editing = ref(null)

const emptyForm = () => ({
  label: '',
  username: '',
  password: '',
  host: 'smtp.qq.com',
  port: 465,
  use_tls: true,
  activate: false,
})

const form = reactive(emptyForm())

const activeLabel = computed(() => {
  const active = accounts.value.find((a) => a.is_active)
  return active ? `${active.label}（${active.username}）` : ''
})

const statusHint = computed(() => {
  if (status.value.effective === 'account') {
    return t('senders.hintAccount')
  }
  if (status.value.effective === 'env') {
    return t('senders.hintEnv')
  }
  return t('senders.hintSimulated')
})

const load = async () => {
  loading.value = true
  try {
    const [list, effective] = await Promise.all([sendersApi.list(), sendersApi.effective()])
    accounts.value = list
    status.value = effective
  } catch (e) {
    ElMessage.error(e.userMessage || t('senders.loadFailed'))
  } finally {
    loading.value = false
  }
}

onMounted(load)

const showDialog = (row) => {
  editing.value = row || null
  if (row) {
    Object.assign(form, {
      label: row.label,
      username: row.username,
      // Never pre-filled: the server keeps the stored one when this is sent
      // empty, and a password that appears in a form is a password that ends
      // up in a screenshot.
      password: '',
      host: row.host,
      port: row.port,
      use_tls: row.use_tls,
      activate: false,
    })
  } else {
    Object.assign(form, emptyForm())
  }
  dialogVisible.value = true
}

const handleSave = async () => {
  if (!form.label || !form.username || (!editing.value && !form.password)) {
    ElMessage.warning(t('senders.required'))
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await sendersApi.update(editing.value.id, form)
      ElMessage.success(t('senders.updated'))
    } else {
      await sendersApi.create(form)
      ElMessage.success(t('senders.created'))
    }
    dialogVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(e.userMessage || t('senders.saveFailed'))
  } finally {
    saving.value = false
  }
}

const handleActivate = async (row) => {
  try {
    await sendersApi.activate(row.id)
    ElMessage.success(t('senders.switched', { label: row.label }))
    await load()
  } catch (e) {
    ElMessage.error(e.userMessage || t('senders.switchFailed'))
  }
}

const handleDelete = async (row) => {
  // Deleting the active mailbox is the one delete with a consequence the
  // table cannot show: nothing is promoted, so sends fall back to .env -- or
  // to simulated. Said out loud, in the confirm, or it is a surprise later.
  const warning = row.is_active
    ? h('p', { style: 'color: var(--el-color-danger); margin: 8px 0 0' }, t('senders.deleteActiveWarning'))
    : null
  try {
    await ElMessageBox.confirm(
      h('div', [
        h('p', { style: 'margin: 0' }, t('senders.deleteConfirm', { label: row.label })),
        warning,
      ]),
      t('senders.deleteTitle'),
      { type: 'warning' },
    )
  } catch (e) {
    return // the human cancelled; nothing was asked of the API
  }

  try {
    await sendersApi.remove(row.id)
    ElMessage.success(t('senders.deleted'))
    await load()
  } catch (e) {
    ElMessage.error(e.userMessage || t('senders.deleteFailed'))
  }
}
</script>

<style scoped>
.effective {
  display: flex;
  align-items: center;
  gap: 12px;
}

.effective-label {
  font-weight: 600;
  font-size: 14px;
}

.effective-hint {
  margin: 10px 0 0;
  color: var(--text-2);
  font-size: 13px;
  line-height: 1.6;
}
</style>
