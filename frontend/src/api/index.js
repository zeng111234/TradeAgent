import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 120000,
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

//: How long two identical sends count as the same send. Long enough for a
//: double click, a slow retry and a second tab; short enough that deliberately
//: sending the same text again a minute later works.
const SEND_WINDOW_MS = 60000

/**
 * An idempotency key for one logical send.
 *
 * The server can only recognise a repeat if the *caller* sends the same key
 * twice, and a fresh random key per call would make every double click a
 * different request -- protecting nothing. So the key is derived from what is
 * being sent plus the current minute: two identical sends inside the window are
 * the same logical send, and the second one comes back as the first one's
 * record (`replayed: true`) instead of a second email.
 *
 * Deliberately not cryptographic: the only requirement is that the same payload
 * in the same minute maps to the same string. Collisions across *different*
 * payloads would be a problem, which is why the hash is over every field that
 * decides what the recipient sees, and the length is included.
 */
export function sendKey(data, now = Date.now()) {
  const parts = [
    data?.customer_id ?? '',
    data?.to_email ?? '',
    data?.subject ?? '',
    data?.body ?? '',
    data?.template_id ?? '',
  ].join('\u0000')

  // FNV-1a, 32-bit.
  let hash = 0x811c9dc5
  for (let i = 0; i < parts.length; i += 1) {
    hash ^= parts.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }

  const bucket = Math.floor(now / SEND_WINDOW_MS)
  return `web-${bucket.toString(36)}-${hash.toString(16)}-${parts.length.toString(36)}`
}

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
  // Every send carries an idempotency key, so a repeated call is a replay the
  // server can recognise rather than a second email. Pass an explicit key to
  // override (e.g. to retry a send that failed and must be attempted again).
  send: (data, options = {}) =>
    api.post('/emails/send', data, {
      headers: { 'Idempotency-Key': options.idempotencyKey || sendKey(data) },
    }),
  getLogs: (params) => api.get('/emails/logs', { params }),
  getStats: () => api.get('/emails/stats'),
  // Pending draft management
  sendPending: () => api.post('/emails/send-pending'),
  sendSinglePending: (id) => api.post(`/emails/send-pending/${id}`),
  updateLog: (id, data) => api.put(`/emails/logs/${id}`, data),
  deleteLog: (id) => api.delete(`/emails/logs/${id}`),
  // The only write path into the 'replied' state. Without it, every "no reply"
  // rule described a state the app could not tell apart from an answered one.
  recordReply: (id, data) => api.post(`/emails/logs/${id}/reply`, data || {}),
  // Turn down a waiting proposal. Kept, not deleted, so the next plan knows.
  rejectDraft: (id, data) => api.post(`/emails/logs/${id}/reject`, data || {}),
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
  // Two-step scan & send with review
  scanLeadsDraft: (data) => api.post('/agent/scan-leads-draft', data),
  sendDraftedEmails: (data) => api.post('/agent/send-drafted-emails', data),
}

// --- Scheduler ---
export const schedulerApi = {
  getJobs: () => api.get('/scheduler/jobs'),
  runDaily: (params) => api.post('/scheduler/run-daily', null, { params }),
}

// --- Agent runs (goal-driven, bounded, fully traced) ---
export const runApi = {
  // What the agent may and may not do. The boundary is published rather than
  // left to be inferred.
  capabilities: () => api.get('/agent/capabilities'),
  // Synchronous: holds until the run finishes or hits its wall-clock budget.
  run: (data) => api.post('/agent/run', data),
  list: (params) => api.get('/agent/runs', { params }),
  get: (runId) => api.get(`/agent/runs/${runId}`),
  // Standalone HTML page, openable in a new tab without this frontend.
  traceUrl: (runId) => `/api/v1/agent/runs/${runId}/trace.html`,
}

export default api
