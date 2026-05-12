<template>
  <div>
    <div class="page-header">
      <h2>待办事项</h2>
      <p>Manage follow-up tasks and reminders</p>
    </div>

    <!-- Toolbar -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <el-row :gutter="16" align="middle">
        <el-col :span="6">
          <el-select v-model="filterStatus" placeholder="Status" clearable>
            <el-option label="Pending" value="pending" />
            <el-option label="In Progress" value="in_progress" />
            <el-option label="Completed" value="completed" />
            <el-option label="Cancelled" value="cancelled" />
          </el-select>
        </el-col>
        <el-col :span="6">
          <el-select v-model="filterPriority" placeholder="Priority" clearable>
            <el-option label="Urgent" value="urgent" />
            <el-option label="High" value="high" />
            <el-option label="Medium" value="medium" />
            <el-option label="Low" value="low" />
          </el-select>
        </el-col>
        <el-col :span="12" style="text-align: right">
          <el-button type="warning" @click="loadOverdue">
            <el-icon><Warning /></el-icon> Overdue ({{ overdueCount }})
          </el-button>
          <el-button type="success" @click="showDialog()">
            <el-icon><Plus /></el-icon> New Task
          </el-button>
        </el-col>
      </el-row>
    </el-card>

    <!-- Task List -->
    <el-card shadow="never">
      <el-table :data="tasks" stripe v-loading="loading">
        <el-table-column prop="title" label="Task" min-width="250">
          <template #default="{ row }">
            <span :style="{ textDecoration: row.status === 'completed' ? 'line-through' : 'none', color: row.status === 'completed' ? '#c0c4cc' : '#303133' }">
              {{ row.title }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="task_type" label="Type" width="100">
          <template #default="{ row }">
            <el-tag size="small">{{ row.task_type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="priority" label="Priority" width="100">
          <template #default="{ row }">
            <el-tag :type="priorityType(row.priority)" size="small">{{ row.priority }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="Status" width="110">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Due Date" width="160">
          <template #default="{ row }">
            <span :style="{ color: isOverdue(row) ? '#f56c6c' : '#606266' }">
              {{ row.due_date ? formatDate(row.due_date) : '-' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="Actions" width="200" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.status === 'pending'" size="small" type="success" link @click="completeTask(row)">Complete</el-button>
            <el-button size="small" type="primary" link @click="showDialog(row)">Edit</el-button>
            <el-button size="small" type="danger" link @click="handleDelete(row)">Delete</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!tasks.length && !loading" description="No tasks" />
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
import { ref, reactive, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { taskApi } from '../api'

const tasks = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editing = ref(null)
const overdueCount = ref(0)
const filterStatus = ref('')
const filterPriority = ref('')

const form = reactive({
  title: '',
  description: '',
  task_type: 'follow_up',
  priority: 'medium',
  status: 'pending',
  due_date: null,
  customer_id: null,
})

const priorityType = (p) => ({ urgent: 'danger', high: 'warning', medium: '', low: 'info' }[p] || '')
const statusType = (s) => ({ pending: 'warning', in_progress: '', completed: 'success', cancelled: 'info' }[s] || '')
const formatDate = (d) => d ? new Date(d).toLocaleString() : ''
const isOverdue = (row) => row.due_date && row.status === 'pending' && new Date(row.due_date) < new Date()

const loadTasks = async () => {
  loading.value = true
  try {
    tasks.value = await taskApi.list({
      status: filterStatus.value || undefined,
      priority: filterPriority.value || undefined,
    })
  } catch (e) {
    ElMessage.error('Failed to load tasks')
  } finally {
    loading.value = false
  }
}

const loadOverdue = async () => {
  try {
    const overdue = await taskApi.getOverdue()
    tasks.value = overdue
    ElMessage.info(`${overdue.length} overdue task(s)`)
  } catch (e) {
    ElMessage.error('Failed to load overdue tasks')
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
    ElMessage.error('Failed to save task')
  } finally {
    saving.value = false
  }
}

const completeTask = async (task) => {
  try {
    await taskApi.update(task.id, { status: 'completed' })
    ElMessage.success('Task completed!')
    loadTasks()
  } catch (e) {
    ElMessage.error('Failed to complete task')
  }
}

const handleDelete = async (task) => {
  try {
    await ElMessageBox.confirm(`Delete task "${task.title}"?`, 'Confirm', { type: 'warning' })
    await taskApi.delete(task.id)
    ElMessage.success('Task deleted')
    loadTasks()
  } catch (e) { /* cancelled */ }
}

onMounted(async () => {
  await loadTasks()
  try {
    const overdue = await taskApi.getOverdue()
    overdueCount.value = overdue.length
  } catch (e) { /* ignore */ }
})
</script>