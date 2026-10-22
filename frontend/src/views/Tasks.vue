<template>
  <div>
    <div class="page-header">
      <h2>{{ t('tasks.title') }}</h2>
      <p>{{ t('tasks.subtitle') }}</p>
    </div>

    <!-- Toolbar -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <el-row :gutter="16" align="middle">
        <el-col :span="6">
          <el-select v-model="filterStatus" :placeholder="t('tasks.filterStatus')" clearable>
            <el-option label="Pending" value="pending" />
            <el-option label="In Progress" value="in_progress" />
            <el-option label="Completed" value="completed" />
            <el-option label="Cancelled" value="cancelled" />
          </el-select>
        </el-col>
        <el-col :span="6">
          <el-select v-model="filterPriority" :placeholder="t('tasks.filterPriority')" clearable>
            <el-option label="Urgent" value="urgent" />
            <el-option label="High" value="high" />
            <el-option label="Medium" value="medium" />
            <el-option label="Low" value="low" />
          </el-select>
        </el-col>
        <el-col :span="12" style="text-align: right">
          <el-button @click="toggleOverdue">
            <el-icon><Warning /></el-icon>
            {{ overdueOnly ? t('tasks.showAll') : t('tasks.overdue', { n: overdueCount }) }}
          </el-button>
          <el-button type="primary" @click="showDialog()">
            <el-icon><Plus /></el-icon> {{ t('tasks.newTask') }}
          </el-button>
        </el-col>
      </el-row>
    </el-card>

    <!-- Task List -->
    <el-card shadow="never">
      <el-table :data="visibleTasks" stripe v-loading="loading">
        <el-table-column prop="title" :label="t('tasks.colTask')" min-width="250">
          <template #default="{ row }">
            <span :style="{ textDecoration: row.status === 'completed' ? 'line-through' : 'none', color: row.status === 'completed' ? 'var(--text-disabled)' : 'var(--text-1)' }">
              {{ row.title }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="task_type" :label="t('tasks.colType')" width="110">
          <template #default="{ row }">
            <!-- task_type is a category, not a status: quiet tag, label from the
                 map (the data carries both `follow_up` and `FOLLOW_UP`). -->
            <el-tag size="small" type="info">{{ taskTypeLabel(row.task_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="priority" :label="t('tasks.colPriority')" width="100">
          <template #default="{ row }">
            <el-tag :type="priorityType(row.priority)" size="small">{{ priorityLabel(row.priority) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" :label="t('tasks.colStatus')" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ taskStatusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('tasks.colDue')" width="160">
          <template #default="{ row }">
            <span :style="{ color: isOverdue(row) ? 'var(--negative)' : 'var(--text-2)' }">
              {{ row.due_date ? formatDate(row.due_date) : '-' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('tasks.colActions')" width="200" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.status === 'pending'" size="small" link @click="completeTask(row)">{{ t('tasks.complete') }}</el-button>
            <el-button size="small" type="primary" link @click="showDialog(row)">{{ t('common.edit') }}</el-button>
            <el-button size="small" type="danger" link @click="handleDelete(row)">{{ t('common.delete') }}</el-button>
          </template>
        </el-table-column>

        <!-- The table used to say "No Data" (in English, from Element Plus)
             and then a second `el-empty` underneath said "No tasks" -- the same
             fact twice, in the wrong language, with no way forward. -->
        <template #empty>
          <el-empty v-if="loaded && !loading" :description="emptyText">
            <el-button v-if="overdueOnly" @click="toggleOverdue">{{ t('tasks.showAll') }}</el-button>
            <el-button v-else-if="filtersActive" @click="clearFilters">{{ t('common.clearFilters') }}</el-button>
            <el-button v-else type="primary" @click="showDialog()">{{ t('tasks.newTask') }}</el-button>
          </el-empty>
        </template>
      </el-table>
    </el-card>

    <!-- Add/Edit Dialog -->
    <el-dialog v-model="dialogVisible" :title="editing ? 'Edit Task' : 'New Task'" width="500px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="Title" required>
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="Description">
          <el-input v-model="form.description" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="Type">
          <el-select v-model="form.task_type" style="width: 100%">
            <el-option label="Follow Up" value="follow_up" />
            <el-option label="Call" value="call" />
            <el-option label="Email" value="email" />
            <el-option label="Meeting" value="meeting" />
            <el-option label="Quotation" value="quotation" />
            <el-option label="Sample" value="sample" />
          </el-select>
        </el-form-item>
        <el-form-item label="Priority">
          <el-select v-model="form.priority" style="width: 100%">
            <el-option label="Urgent" value="urgent" />
            <el-option label="High" value="high" />
            <el-option label="Medium" value="medium" />
            <el-option label="Low" value="low" />
          </el-select>
        </el-form-item>
        <el-form-item label="Status" v-if="editing">
          <el-select v-model="form.status" style="width: 100%">
            <el-option label="Pending" value="pending" />
            <el-option label="In Progress" value="in_progress" />
            <el-option label="Completed" value="completed" />
            <el-option label="Cancelled" value="cancelled" />
          </el-select>
        </el-form-item>
        <el-form-item label="Due Date">
          <el-date-picker v-model="form.due_date" type="datetime" style="width: 100%" />
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { taskApi } from '../api'
import { priorityLabel, priorityType, taskStatusLabel, taskStatusType as statusType, taskTypeLabel } from '../constants/status.js'
import { t } from '../i18n/index.js'
import { failureDetail, patchRow, reconcile, removeRow } from '../utils/optimistic'

const tasks = ref([])
const loading = ref(false)
// Set on a successful read only: a failed one leaves us not knowing whether the
// list is empty, and the table must not answer that question with a guess.
const loaded = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editing = ref(null)
const overdueCount = ref(0)
const filterStatus = ref('')
const filterPriority = ref('')

// The overdue list used to be written into `tasks`, which replaced the list the
// user was working in and left no way back to it. It is a *view*, so it gets its
// own storage and a toggle, and `tasks` is never overwritten by it.
const overdueOnly = ref(false)
const overdueTasks = ref([])

const visibleTasks = computed(() =>
  overdueOnly.value ? overdueTasks.value : tasks.value
)

const filtersActive = computed(() => Boolean(filterStatus.value || filterPriority.value))

// Three different reasons the table can be empty, three different ways out:
// overdue is a filtered view, a status filter is the user's own doing, and the
// rest is a genuinely empty list.
const emptyText = computed(() => {
  if (overdueOnly.value) return t('tasks.emptyOverdue')
  if (filtersActive.value) return t('common.noResults')
  return t('tasks.empty')
})

const clearFilters = () => {
  filterStatus.value = ''
  filterPriority.value = ''
}

const form = reactive({
  title: '',
  description: '',
  task_type: 'follow_up',
  priority: 'medium',
  status: 'pending',
  due_date: null,
  customer_id: null,
})

// Minute precision: a due date does not have seconds, and "08:20:00" is noise.
const formatDate = (d) => d
  ? new Date(d).toLocaleString([], { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  : ''
const isOverdue = (row) => row.due_date && row.status === 'pending' && new Date(row.due_date) < new Date()

const loadTasks = async () => {
  loading.value = true
  try {
    tasks.value = await taskApi.list({
      status: filterStatus.value || undefined,
      priority: filterPriority.value || undefined,
    })
    loaded.value = true
  } catch (e) {
    ElMessage.error('Failed to load tasks')
  } finally {
    loading.value = false
  }
}

const toggleOverdue = async () => {
  if (overdueOnly.value) {
    overdueOnly.value = false
    return
  }
  loading.value = true
  try {
    overdueTasks.value = await taskApi.getOverdue()
    overdueCount.value = overdueTasks.value.length
    overdueOnly.value = true
    loaded.value = true
    ElMessage.info(`${overdueTasks.value.length} overdue task(s)`)
  } catch (e) {
    ElMessage.error(failureDetail(e, 'Failed to load overdue tasks'))
  } finally {
    loading.value = false
  }
}

watch([filterStatus, filterPriority], loadTasks)

const showDialog = (task) => {
  if (task) {
    editing.value = task
    Object.assign(form, {
      title: task.title, description: task.description || '', task_type: task.task_type,
      priority: task.priority, status: task.status, due_date: task.due_date, customer_id: task.customer_id,
    })
  } else {
    editing.value = null
    Object.assign(form, { title: '', description: '', task_type: 'follow_up', priority: 'medium', status: 'pending', due_date: null, customer_id: null })
  }
  dialogVisible.value = true
}

const handleSave = async () => {
  if (!form.title.trim()) { ElMessage.warning('Title is required'); return }
  saving.value = true
  try {
    if (editing.value) {
      await taskApi.update(editing.value.id, form)
      ElMessage.success('Task updated')
    } else {
      await taskApi.create(form)
      ElMessage.success('Task created')
    }
    dialogVisible.value = false
    loadTasks()
  } catch (e) {
    ElMessage.error(failureDetail(e, 'Failed to save task'))
  } finally {
    saving.value = false
  }
}

/**
 * Completing is optimistic because it is a one-click action on a row the user is
 * looking at: waiting 5 ms locally / a few hundred on a phone to see a line
 * through a task reads as "the click did not register".
 *
 * The value written is only `status`. `completed_at` is left to the server --
 * guessing a timestamp here would be a value the client invented, and rolling it
 * back later would be undoing something it never knew.
 */
const completeTask = async (task) => {
  // Read before the patch: `isOverdue` looks at `status`, and the optimistic
  // value has already changed it by the time the request returns.
  const wasOverdue = isOverdue(task)
  const list = overdueOnly.value ? overdueTasks.value : tasks.value
  const { undo } = patchRow(list, task.id, { status: 'completed' })

  try {
    const saved = await taskApi.update(task.id, { status: 'completed' })
    reconcile(list, saved)
    // Only after the server agrees does the row stop matching the current view.
    // Removing it optimistically would need a second undo, and a failure would
    // have to put the row back *and* restore its status.
    if (overdueOnly.value || filterStatus.value === 'pending') {
      removeRow(list, task.id)
    }
    if (wasOverdue) overdueCount.value = Math.max(0, overdueCount.value - 1)
    ElMessage.success('Task completed!')
  } catch (e) {
    if (e?.response?.status === 404) {
      // The row is gone on the server. Rolling its status back would leave a
      // task in the list that does not exist, so the honest move is to drop it.
      removeRow(list, task.id)
      ElMessage.info('That task no longer exists')
      return
    }
    if (!undo()) {
      // A newer write owns this row now; the local value cannot be trusted.
      await loadTasks()
    }
    ElMessage.error(failureDetail(e, 'Failed to complete task'))
  }
}

const handleDelete = async (task) => {
  try {
    await ElMessageBox.confirm(`Delete task "${task.title}"?`, 'Confirm', { type: 'warning' })
  } catch (e) {
    return                              // the human cancelled; nothing was asked of the API
  }

  const list = overdueOnly.value ? overdueTasks.value : tasks.value
  const { undo } = removeRow(list, task.id)
  try {
    await taskApi.delete(task.id)
    ElMessage.success('Task deleted')
  } catch (e) {
    // 404 means the row is gone, which is the outcome the user asked for.
    // Putting it back would show a task the server does not have.
    if (e?.response?.status === 404) {
      ElMessage.info('That task was already deleted')
      return
    }
    undo()
    ElMessage.error(failureDetail(e, 'Failed to delete task'))
  }
}

onMounted(async () => {
  await loadTasks()
  try {
    const overdue = await taskApi.getOverdue()
    overdueCount.value = overdue.length
  } catch (e) { /* ignore */ }
})
</script>