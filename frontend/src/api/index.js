import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// Response interceptor
api.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const message = error.response?.data?.detail || error.message || 'Request failed'
    console.error('API Error:', message)
    return Promise.reject(error)
  }
)

// --- Customers ---
export const customerApi = {
  list: (params) => api.get('/customers', { params }),
  get: (id) => api.get(`/customers/${id}`),
  create: (data) => api.post('/customers', data),
  update: (id, data) => api.put(`/customers/${id}`, data),
  delete: (id) => api.delete(`/customers/${id}`),
  import: (items) => api.post('/customers/import', items),
  addContact: (id, data) => api.post(`/customers/${id}/contacts`, data),
  getContacts: (id) => api.get(`/customers/${id}/contacts`),
  addNote: (id, data) => api.post(`/customers/${id}/notes`, data),
  getNotes: (id) => api.get(`/customers/${id}/notes`),
}

// --- Emails ---
export const emailApi = {
  getTemplates: (params) => api.get('/emails/templates', { params }),
  getTemplate: (id) => api.get(`/emails/templates/${id}`),
  createTemplate: (data) => api.post('/emails/templates', data),
  updateTemplate: (id, data) => api.put(`/emails/templates/${id}`, data),
  deleteTemplate: (id) => api.delete(`/emails/templates/${id}`),
  generate: (data) => api.post('/emails/generate', data),
  send: (data) => api.post('/emails/send', data),
  getLogs: (params) => api.get('/emails/logs', { params }),
  getStats: () => api.get('/emails/stats'),
}

// --- Tasks ---
export const taskApi = {
  list: (params) => api.get('/tasks', { params }),
  get: (id) => api.get(`/tasks/${id}`),
  create: (data) => api.post('/tasks', data),
  update: (id, data) => api.put(`/tasks/${id}`, data),
  delete: (id) => api.delete(`/tasks/${id}`),
  getOverdue: () => api.get('/tasks/overdue/list'),
}

// --- Analytics ---
export const analyticsApi = {
  dashboard: () => api.get('/analytics/dashboard'),
  pipeline: () => api.get('/analytics/pipeline'),
  sources: () => api.get('/analytics/sources'),
  countries: () => api.get('/analytics/countries'),
  trends: (months = 6) => api.get('/analytics/trends', { params: { months } }),
}

// --- AI Agent ---
export const agentApi = {
  analyzeWebsite: (data) => api.post('/agent/analyze-website', data),
  analyzeEmail: (data) => api.post('/agent/analyze-email', data),
  negotiationAdvice: (data) => api.post('/agent/negotiation-advice', data),
  dailyIntelligence: () => api.post('/agent/daily-intelligence'),
  batchEmails: (data) => api.post('/agent/batch-emails', data),
  // New features
  analyzeInquiry: (data) => api.post('/agent/analyze-inquiry', data),
  churnAlerts: () => api.post('/agent/churn-alerts'),
  getHolidays: () => api.get('/agent/holidays'),
  generateHolidayEmails: (data) => api.post('/agent/holiday-emails', data),
  scanLeads: (data) => api.post('/agent/scan-leads', data),
  generatePI: (data) => api.post('/agent/generate-pi', data),
  // Import daily scan leads
  previewLeads: () => api.get('/agent/import-leads/preview'),
  importLeads: () => api.post('/agent/import-leads'),
}

// --- Scheduler ---
export const schedulerApi = {
  getJobs: () => api.get('/scheduler/jobs'),
  runDaily: (params) => api.post('/scheduler/run-daily', null, { params }),
}

export default api
