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

export default api