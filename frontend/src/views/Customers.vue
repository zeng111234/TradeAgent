<template>
  <div>
    <div class="page-header">
      <h2>{{ t('customers.title') }}</h2>
      <p>{{ t('customers.subtitle') }}</p>
    </div>

    <!-- Toolbar: flex-wrap, so the filters stack instead of squeezing below
         1024. The old el-col spans broke at a fixed ratio whatever the width. -->
    <el-card shadow="never" class="mb-4">
      <div class="toolbar">
        <el-input v-model="filters.search" :placeholder="t('customers.searchPlaceholder')" clearable class="filter-search" @keyup.enter="loadCustomers">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="filters.stage" :placeholder="t('customers.stageAll')" clearable class="filter-select">
          <el-option :label="t('status.stage.new')" value="new" />
          <el-option :label="t('status.stage.contacted')" value="contacted" />
          <el-option :label="t('status.stage.interested')" value="interested" />
          <el-option :label="t('status.stage.quoting')" value="quoting" />
          <el-option :label="t('status.stage.sample')" value="sample" />
          <el-option :label="t('status.stage.ordering')" value="ordering" />
          <el-option :label="t('status.stage.completed')" value="completed" />
          <el-option :label="t('status.stage.lost')" value="lost" />
        </el-select>
        <el-select v-model="filters.source" :placeholder="t('customers.sourceAll')" clearable class="filter-select">
          <el-option label="Manual" value="manual" />
          <el-option label="Import" value="import" />
          <el-option label="Scraper" value="scraper" />
          <el-option label="Alibaba" value="alibaba" />
          <el-option label="Exhibition" value="exhibition" />
          <el-option label="Referral" value="referral" />
        </el-select>
        <el-select v-model="filters.country" :placeholder="t('customers.countryAll')" clearable filterable class="filter-select filter-select--wide">
          <el-option v-for="c in countries" :key="c" :label="c" :value="c" />
        </el-select>
        <div class="toolbar__end">
          <el-button @click="loadCustomers">
            <el-icon><Search /></el-icon> {{ t('common.search') }}
          </el-button>
          <el-button type="primary" @click="showAddDialog">
            <el-icon><Plus /></el-icon> {{ t('customers.addCustomer') }}
          </el-button>
        </div>
      </div>
    </el-card>

    <!-- Customer Table. Column budget 834: Country folds into a second line
         under Company, and the dead 140px Industry width was what wrapped
         every row to two lines. -->
    <el-card shadow="never">
      <el-table :data="customers" stripe v-loading="loading" @row-click="goToDetail">
        <el-table-column prop="company_name" :label="t('customers.colCompany')" min-width="220">
          <template #default="{ row }">
            <div class="cell-stack">
              <span class="cell-truncate" :title="row.company_name">{{ row.company_name }}</span>
              <span v-if="row.country" class="hint">{{ row.country }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="industry" :label="t('customers.colIndustry')" min-width="150" show-overflow-tooltip />
        <el-table-column prop="stage" :label="t('customers.colStage')" width="100">
          <template #default="{ row }">
            <el-tag :type="stageTagType(row.stage)" size="small">{{ stageLabel(row.stage) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="source" :label="t('customers.colSource')" width="100">
          <template #default="{ row }">
            <span class="text-muted">{{ row.source }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="score" :label="t('customers.colScore')" width="70" align="center">
          <template #default="{ row }">
            <span class="num" :class="scoreClass(row.score)">{{ row.score }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('customers.colContacts')" width="84" align="center">
          <template #default="{ row }">
            {{ row.contacts ? row.contacts.length : 0 }}
          </template>
        </el-table-column>
        <el-table-column :label="t('customers.colActions')" width="130" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" link @click.stop="showEditDialog(row)">{{ t('common.edit') }}</el-button>
            <el-button size="small" type="danger" link @click.stop="handleDelete(row)">{{ t('common.delete') }}</el-button>
          </template>
        </el-table-column>

        <!-- Element Plus's own empty block says "No Data" in whatever locale it
             was installed with. This one says why the table is empty and what
             to do about it -- and `customers.empty` had been sitting unused in
             the dictionary since it was written. -->
        <template #empty>
          <el-empty v-if="loaded && !loading" :description="filtersActive ? t('common.noResults') : t('customers.empty')">
            <el-button v-if="filtersActive" @click="clearFilters">{{ t('common.clearFilters') }}</el-button>
            <el-button v-else type="primary" @click="showAddDialog">{{ t('customers.addCustomer') }}</el-button>
          </el-empty>
        </template>
      </el-table>

      <!-- Pagination -->
      <el-pagination
        v-if="total > 0"
        class="table-pager"
        :current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="handlePageChange"
      />
    </el-card>

    <!-- Add/Edit Dialog -->
    <el-dialog v-model="dialogVisible" :title="editingCustomer ? t('customers.editCustomer') : t('customers.addCustomerTitle')" width="600px">
      <el-form :model="form" label-width="120px">
        <el-form-item :label="t('customers.colCompany') + ' *'" required>
          <el-input v-model="form.company_name" />
        </el-form-item>
        <el-form-item :label="t('common.country')">
          <el-input v-model="form.country" />
        </el-form-item>
        <el-form-item label="City">
          <el-input v-model="form.city" />
        </el-form-item>
        <el-form-item :label="t('dashboard.colWebsite')">
          <el-input v-model="form.website" placeholder="https://" />
        </el-form-item>
        <el-form-item :label="t('common.industry')">
          <el-input v-model="form.industry" />
        </el-form-item>
        <el-form-item label="Products">
          <el-input v-model="form.products" type="textarea" :rows="2" placeholder="Comma separated keywords" />
        </el-form-item>
        <el-form-item :label="t('common.stage')">
          <el-select v-model="form.stage" class="form-fill">
            <el-option :label="t('status.stage.new')" value="new" />
            <el-option :label="t('status.stage.contacted')" value="contacted" />
            <el-option :label="t('status.stage.interested')" value="interested" />
            <el-option :label="t('status.stage.quoting')" value="quoting" />
            <el-option :label="t('status.stage.sample')" value="sample" />
            <el-option :label="t('status.stage.ordering')" value="ordering" />
            <el-option :label="t('status.stage.completed')" value="completed" />
            <el-option :label="t('status.stage.lost')" value="lost" />
          </el-select>
        </el-form-item>
        <el-form-item label="Tags">
          <el-input v-model="form.tags" placeholder="Comma separated tags" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" @click="handleSave" :loading="saving">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { customerApi } from '../api'
import { scoreClass, stageLabel, stageTagType } from '../constants/status.js'
import { t } from '../i18n/index.js'

const router = useRouter()
const loading = ref(false)
// Only a *successful* read earns this. A failed request means we do not know
// whether the list is empty, so saying "no customers yet" would be inventing an
// answer -- the table stays with Element Plus's neutral default instead. It also
// keeps the empty state off the first frame, before any request has started.
const loaded = ref(false)
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

// "Nothing here" and "nothing matched" are different sentences with different
// ways out, so the empty state has to know which one it is looking at.
const filtersActive = computed(() =>
  Boolean(filters.search || filters.stage || filters.source || filters.country))

const clearFilters = () => {
  Object.assign(filters, { search: '', stage: '', source: '', country: '' })
  page.value = 1
  loadCustomers()
}

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
    loaded.value = true
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
      // Send back the updated_at we loaded: if someone else saved this customer
      // in the meantime the API refuses with 409 instead of overwriting them.
      await customerApi.update(editingCustomer.value.id, {
        ...form,
        expected_updated_at: editingCustomer.value.updated_at,
      })
      ElMessage.success('Customer updated')
    } else {
      await customerApi.create(form)
      ElMessage.success('Customer created')
    }
    dialogVisible.value = false
    loadCustomers()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || 'Failed to save customer')
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
    // User cancelled (a bare reject) or the API refused -- a customer with
    // sent mail is kept, and the API says why. Show that instead of silence.
    if (e !== 'cancel' && e !== 'close' && e?.response) {
      ElMessage.error(e?.response?.data?.detail || 'Failed to delete customer')
    }
  }
}

onMounted(loadCustomers)
</script>