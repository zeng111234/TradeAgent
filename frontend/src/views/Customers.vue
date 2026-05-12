<template>
  <div>
    <div class="page-header">
      <h2>客户管理</h2>
      <p>Manage your foreign trade customers and contacts</p>
    </div>

    <!-- Toolbar -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <el-row :gutter="16" align="middle">
        <el-col :span="6">
          <el-input v-model="filters.search" placeholder="Search company, industry..." clearable @keyup.enter="loadCustomers">
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
        </el-col>
        <el-col :span="4">
          <el-select v-model="filters.stage" placeholder="Stage" clearable>
            <el-option label="New" value="new" />
            <el-option label="Contacted" value="contacted" />
            <el-option label="Interested" value="interested" />
            <el-option label="Quoting" value="quoting" />
            <el-option label="Sample" value="sample" />
            <el-option label="Ordering" value="ordering" />
            <el-option label="Completed" value="completed" />
            <el-option label="Lost" value="lost" />
          </el-select>
        </el-col>
        <el-col :span="4">
          <el-select v-model="filters.source" placeholder="Source" clearable>
            <el-option label="Manual" value="manual" />
            <el-option label="Import" value="import" />
            <el-option label="Scraper" value="scraper" />
            <el-option label="Alibaba" value="alibaba" />
            <el-option label="Exhibition" value="exhibition" />
            <el-option label="Referral" value="referral" />
          </el-select>
        </el-col>
        <el-col :span="4">
          <el-select v-model="filters.country" placeholder="Country" clearable filterable>
            <el-option v-for="c in countries" :key="c" :label="c" :value="c" />
          </el-select>
        </el-col>
        <el-col :span="6" style="text-align: right">
          <el-button type="primary" @click="loadCustomers">
            <el-icon><Search /></el-icon> Search
          </el-button>
          <el-button type="success" @click="showAddDialog">
            <el-icon><Plus /></el-icon> Add Customer
          </el-button>
        </el-col>
      </el-row>
    </el-card>

    <!-- Customer Table -->
    <el-card shadow="never">
      <el-table :data="customers" stripe style="width: 100%" v-loading="loading" @row-click="goToDetail">
        <el-table-column prop="company_name" label="Company" min-width="180" />
        <el-table-column prop="country" label="Country" width="120" />
        <el-table-column prop="industry" label="Industry" width="140" />
        <el-table-column prop="stage" label="Stage" width="110">
          <template #default="{ row }">
            <el-tag :type="stageTagType(row.stage)" size="small">{{ stageLabel(row.stage) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="Source" width="100">
          <template #default="{ row }">
            <el-tag type="info" size="small">{{ row.source }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="score" label="Score" width="80" align="center">
          <template #default="{ row }">
            <span :style="{ color: row.score >= 70 ? '#67c23a' : row.score >= 40 ? '#e6a23c' : '#909399', fontWeight: 600 }">
              {{ row.score }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="Contacts" width="90" align="center">
          <template #default="{ row }">
            {{ row.contacts ? row.contacts.length : 0 }}
          </template>
        </el-table-column>
        <el-table-column label="Actions" width="160" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click.stop="showEditDialog(row)">Edit</el-button>
            <el-button size="small" type="danger" link @click.stop="handleDelete(row)">Delete</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- Pagination -->
      <el-pagination
        v-if="total > 0"
        style="margin-top: 16px; justify-content: flex-end"
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="handlePageChange"
      />
    </el-card>

    <!-- Add/Edit Dialog -->
    <el-dialog v-model="dialogVisible" :title="editingCustomer ? 'Edit Customer' : 'Add Customer'" width="600px">
      <el-form :model="form" label-width="120px">
        <el-form-item label="Company Name" required>
          <el-input v-model="form.company_name" />
        </el-form-item>
        <el-form-item label="Country">
          <el-input v-model="form.country" />
        </el-form-item>
        <el-form-item label="City">
          <el-input v-model="form.city" />
        </el-form-item>
        <el-form-item label="Website">
          <el-input v-model="form.website" placeholder="https://" />
        </el-form-item>
        <el-form-item label="Industry">
          <el-input v-model="form.industry" />
        </el-form-item>
        <el-form-item label="Products">
          <el-input v-model="form.products" type="textarea" :rows="2" placeholder="Comma separated keywords" />
        </el-form-item>
        <el-form-item label="Stage">
          <el-select v-model="form.stage" style="width: 100%">
            <el-option label="New" value="new" />
            <el-option label="Contacted" value="contacted" />
            <el-option label="Interested" value="interested" />
            <el-option label="Quoting" value="quoting" />
            <el-option label="Sample" value="sample" />
            <el-option label="Ordering" value="ordering" />
            <el-option label="Completed" value="completed" />
            <el-option label="Lost" value="lost" />
          </el-select>
        </el-form-item>
        <el-form-item label="Tags">
          <el-input v-model="form.tags" placeholder="Comma separated tags" />
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
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { customerApi } from '../api'

const router = useRouter()
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editingCustomer = ref(null)
const customers = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const countries = ref(['United States', 'Germany', 'United Kingdom', 'Japan', 'Spain', 'France', 'Canada', 'Australia', 'Brazil', 'India'])

const filters = reactive({
  search: '',
  stage: '',
  source: '',
  country: '',
})

const form = reactive({
  company_name: '',
  country: '',
  city: '',
  website: '',
  industry: '',
  products: '',
  stage: 'new',
  tags: '',
})

const stageLabel = (stage) => {
  const map = {
    new: 'New', contacted: 'Contacted', interested: 'Interested',
    quoting: 'Quoting', sample: 'Sample', ordering: 'Ordering',
    completed: 'Completed', lost: 'Lost',
  }
  return map[stage] || stage
}

const stageTagType = (stage) => {
  const map = {
    new: 'info', contacted: '', interested: 'success',
    quoting: 'warning', sample: 'warning', ordering: 'warning',
    completed: 'success', lost: 'danger',
  }
  return map[stage] || ''
}

const loadCustomers = async () => {
  loading.value = true
  try {
    const data = await customerApi.list({
      page: page.value,
      page_size: pageSize.value,
      ...filters,
    })
    customers.value = data.items
    total.value = data.total
  } catch (e) {
    ElMessage.error('Failed to load customers')
  } finally {
    loading.value = false
  }
}

const handlePageChange = (p) => {
  page.value = p
  loadCustomers()
}

const goToDetail = (row) => {
  router.push(`/customers/${row.id}`)
}

const showAddDialog = () => {
  editingCustomer.value = null
  Object.assign(form, { company_name: '', country: '', city: '', website: '', industry: '', products: '', stage: 'new', tags: '' })
  dialogVisible.value = true
}

const showEditDialog = (row) => {
  editingCustomer.value = row
  Object.assign(form, {
    company_name: row.company_name,
    country: row.country || '',
    city: row.city || '',
    website: row.website || '',
    industry: row.industry || '',
    products: row.products || '',
    stage: row.stage,
    tags: row.tags || '',
  })
  dialogVisible.value = true
}

const handleSave = async () => {
  if (!form.company_name.trim()) {
    ElMessage.warning('Company name is required')
    return
  }
  saving.value = true
  try {
    if (editingCustomer.value) {
      await customerApi.update(editingCustomer.value.id, form)
      ElMessage.success('Customer updated')
    } else {
      await customerApi.create(form)
      ElMessage.success('Customer created')
    }
    dialogVisible.value = false
    loadCustomers()
  } catch (e) {
    ElMessage.error('Failed to save customer')
  } finally {
    saving.value = false
  }
}

const handleDelete = async (row) => {
  try {
    await ElMessageBox.confirm(`Delete customer "${row.company_name}"?`, 'Confirm', { type: 'warning' })
    await customerApi.delete(row.id)
    ElMessage.success('Customer deleted')
    loadCustomers()
  } catch (e) {
    // User cancelled
  }
}

onMounted(loadCustomers)
</script>