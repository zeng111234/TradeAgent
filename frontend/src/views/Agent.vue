<template>
  <div>
    <div class="page-header">
      <h2>AI Agent</h2>
      <p>AI-powered tools for smarter foreign trade decisions</p>
    </div>

    <!-- Five Tools Tabs -->
    <el-tabs v-model="activeTab" type="border-card" style="margin-bottom: 20px">

      <!-- Tab 0: Daily Intelligence -->
      <el-tab-pane label="Daily Intelligence" name="daily">
        <el-row :gutter="20">
          <el-col :span="24">
            <el-card shadow="never">
              <template #header>
                <div style="display: flex; justify-content: space-between; align-items: center">
                  <span style="font-weight: 600">AI Daily Intelligence - Who to Follow Up Today</span>
                  <el-button type="primary" @click="runDailyIntelligence" :loading="dailyLoading" size="large">
                    <el-icon><MagicStick /></el-icon> Run Daily Scan
                  </el-button>
                </div>
              </template>

              <el-alert v-if="dailyResult" :title="`Found ${dailyResult.high_priority} high priority and ${dailyResult.medium_priority} medium priority follow-ups`"
                :type="dailyResult.high_priority > 0 ? 'warning' : 'success'" :closable="false" show-icon style="margin-bottom: 16px" />

              <div v-if="dailyResult && dailyResult.actions && dailyResult.actions.length">
                <div v-for="a in dailyResult.actions" :key="a.customer_id"
                  style="border: 1px solid #e4e7ed; border-radius: 8px; padding: 16px; margin-bottom: 12px"
                  :style="{ borderColor: a.priority === 'high' ? '#f56c6c' : (a.priority === 'medium' ? '#e6a23c' : '#e4e7ed'), borderLeftWidth: '4px' }">
                  <el-row :gutter="16">
                    <el-col :span="16">
                      <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px">
                        <el-tag :type="a.priority === 'high' ? 'danger' : (a.priority === 'medium' ? 'warning' : 'info')" size="small">{{ a.priority }}</el-tag>
                        <strong>{{ a.company_name }}</strong>
                        <el-tag size="small" type="info">{{ a.stage }}</el-tag>
                      </div>
                      <p style="margin: 0 0 4px; color: #606266; font-size: 13px">Reason: {{ a.reason }}</p>
                      <p style="margin: 0; color: #409eff; font-size: 13px">Action: {{ a.suggested_action }}</p>
                      <p v-if="a.contact_email" style="margin: 4px 0 0; color: #909399; font-size: 12px">Contact: {{ a.contact_name }} ({{ a.contact_email }})</p>
                    </el-col>
                    <el-col :span="8" style="text-align: right">
                      <el-button v-if="a.draft_email" size="small" type="success" @click="showDraftEmail(a)">View Draft Email</el-button>
                      <el-button v-if="a.contact_email" size="small" type="primary" @click="sendQuickEmail(a)">Quick Send</el-button>
                    </el-col>
                  </el-row>
                  <div v-if="a.showDraft" style="background: #f5f7fa; padding: 12px; border-radius: 6px; margin-top: 12px">
                    <p style="margin: 0 0 4px; font-weight: 600; font-size: 13px">Subject: {{ a.draft_subject }}</p>
                    <div v-html="a.draft_email" style="font-size: 13px; color: #303133"></div>
                  </div>
                </div>
              </div>
              <el-empty v-else-if="dailyResult && !dailyResult.actions?.length" description="No follow-ups needed today - all customers are on track!" />
              <el-empty v-else description="Click 'Run Daily Scan' to let AI analyze all your customers" />
            </el-card>
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 1: Website Analysis -->
      <el-tab-pane label="Website Lead Analysis" name="website">
        <el-row :gutter="20">
          <el-col :span="10">
            <el-card shadow="never">
              <template #header><span style="font-weight: 600">Analyze Customer Website</span></template>
              <el-form label-width="120px">
                <el-form-item label="Website URL" required>
                  <el-input v-model="webForm.url" placeholder="https://www.example.com" />
                </el-form-item>
                <el-form-item label="Your Products">
                  <el-input v-model="webForm.your_products" placeholder="LED lights, solar panels..." />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" @click="analyzeWebsite" :loading="webLoading" size="large">
                    <el-icon><MagicStick /></el-icon> Analyze
                  </el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="14">
            <el-card v-if="webResult" shadow="never">
              <template #header>
                <div style="display: flex; justify-content: space-between; align-items: center">
                  <span style="font-weight: 600">Lead Intelligence Report</span>
                  <el-tag :type="webResult.ai_powered ? 'success' : 'info'" size="small">
                    {{ webResult.ai_powered ? 'AI Powered' : 'Basic Analysis' }}
                  </el-tag>
                </div>
              </template>

              <!-- Score -->
              <div style="text-align: center; margin-bottom: 20px">
                <el-progress type="dashboard" :percentage="webResult.lead_score || 0"
                  :color="scoreColor(webResult.lead_score || 0)" :width="120" />
                <p style="margin-top: 8px; color: #909399; font-size: 13px">Lead Score</p>
              </div>

              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="Company">{{ webResult.company_name }}</el-descriptions-item>
                <el-descriptions-item label="Country">{{ webResult.country }}</el-descriptions-item>
                <el-descriptions-item label="Industry">{{ webResult.industry }}</el-descriptions-item>
                <el-descriptions-item label="Size">{{ webResult.company_size_estimate }}</el-descriptions-item>
                <el-descriptions-item label="Order Potential">{{ webResult.estimated_order_potential }}</el-descriptions-item>
                <el-descriptions-item label="Score Reason" :span="2">{{ webResult.score_reasoning }}</el-descriptions-item>
              </el-descriptions>

              <div v-if="webResult.products_they_sell && webResult.products_they_sell.length" style="margin-top: 16px">
                <h4 style="margin: 0 0 8px; font-size: 14px">Products They Sell/Import:</h4>
                <el-tag v-for="p in webResult.products_they_sell" :key="p" style="margin: 2px 4px">{{ p }}</el-tag>
              </div>

              <div v-if="webResult.likely_buying_interest && webResult.likely_buying_interest.length" style="margin-top: 16px">
                <h4 style="margin: 0 0 8px; font-size: 14px">Likely Buying Interest:</h4>
                <el-tag v-for="p in webResult.likely_buying_interest" :key="p" type="success" style="margin: 2px 4px">{{ p }}</el-tag>
              </div>

              <el-alert v-if="webResult.recommended_approach" :title="'Approach: ' + webResult.recommended_approach"
                type="info" :closable="false" show-icon style="margin-top: 16px" />

              <div v-if="webResult.key_selling_points && webResult.key_selling_points.length" style="margin-top: 16px">
                <h4 style="margin: 0 0 8px; font-size: 14px">Key Selling Points:</h4>
                <ul style="margin: 0; padding-left: 20px">
                  <li v-for="s in webResult.key_selling_points" :key="s" style="color: #606266; font-size: 13px; margin-bottom: 4px">{{ s }}</li>
                </ul>
              </div>

              <div v-if="webResult.risk_factors && webResult.risk_factors.length" style="margin-top: 16px">
                <h4 style="margin: 0 0 8px; font-size: 14px; color: #f56c6c">Risk Factors:</h4>
                <ul style="margin: 0; padding-left: 20px">
                  <li v-for="r in webResult.risk_factors" :key="r" style="color: #f56c6c; font-size: 13px; margin-bottom: 4px">{{ r }}</li>
                </ul>
              </div>
            </el-card>
            <el-empty v-else description="Enter a website URL and click Analyze" />
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 2: Email Reply Analysis -->
      <el-tab-pane label="Email Reply Analysis" name="email">
        <el-row :gutter="20">
          <el-col :span="10">
            <el-card shadow="never">
              <template #header><span style="font-weight: 600">Analyze Customer Reply</span></template>
              <el-form label-width="100px">
                <el-form-item label="Email Content" required>
                  <el-input v-model="emailForm.email_content" type="textarea" :rows="10"
                    placeholder="Paste the customer's email reply here..." />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" @click="analyzeEmail" :loading="emailLoading" size="large">
                    <el-icon><MagicStick /></el-icon> Analyze
                  </el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="14">
            <el-card v-if="emailResult" shadow="never">
              <template #header>
                <div style="display: flex; justify-content: space-between; align-items: center">
                  <span style="font-weight: 600">Email Intelligence</span>
                  <el-tag :type="emailResult.ai_powered ? 'success' : 'info'" size="small">
                    {{ emailResult.ai_powered ? 'AI Powered' : 'Basic' }}
                  </el-tag>
                </div>
              </template>

              <!-- Key Metrics -->
              <el-row :gutter="16" style="margin-bottom: 20px">
                <el-col :span="8">
                  <div class="stat-card" style="text-align: center; padding: 12px">
                    <el-tag :type="intentColor(emailResult.intent)" size="large">{{ emailResult.intent }}</el-tag>
                    <p style="margin-top: 4px; font-size: 12px; color: #909399">Intent</p>
                  </div>
                </el-col>
                <el-col :span="8">
                  <div class="stat-card" style="text-align: center; padding: 12px">
                    <el-tag :type="sentimentColor(emailResult.sentiment)" size="large">{{ emailResult.sentiment }}</el-tag>
                    <p style="margin-top: 4px; font-size: 12px; color: #909399">Sentiment</p>
                  </div>
                </el-col>
                <el-col :span="8">
                  <div class="stat-card" style="text-align: center; padding: 12px">
                    <el-tag :type="urgencyColor(emailResult.urgency)" size="large">{{ emailResult.urgency }}</el-tag>
                    <p style="margin-top: 4px; font-size: 12px; color: #909399">Urgency</p>
                  </div>
                </el-col>
              </el-row>

              <!-- Extracted Info -->
              <el-descriptions v-if="emailResult.extracted_info" :column="2" border size="small" style="margin-bottom: 16px">
                <el-descriptions-item label="Products" :span="2">
                  {{ (emailResult.extracted_info.products_mentioned || []).join(', ') || '-' }}
                </el-descriptions-item>
                <el-descriptions-item label="Quantity">{{ emailResult.extracted_info.quantity_mentioned || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Budget">{{ emailResult.extracted_info.budget_mentioned || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Deadline">{{ emailResult.extracted_info.delivery_deadline || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Target Price">{{ emailResult.extracted_info.target_price || '-' }}</el-descriptions-item>
              </el-descriptions>

              <!-- Recommended Action -->
              <el-alert :title="'Recommended Action: ' + emailResult.recommended_action"
                type="success" :closable="false" show-icon style="margin-bottom: 16px" />

              <!-- Suggested Reply Points -->
              <div v-if="emailResult.suggested_reply_points && emailResult.suggested_reply_points.length">
                <h4 style="margin: 0 0 8px; font-size: 14px">Reply Points:</h4>
                <ul style="margin: 0; padding-left: 20px">
                  <li v-for="s in emailResult.suggested_reply_points" :key="s" style="color: #606266; font-size: 13px; margin-bottom: 4px">{{ s }}</li>
                </ul>
              </div>

              <div v-if="emailResult.risks && emailResult.risks.length" style="margin-top: 12px">
                <h4 style="margin: 0 0 8px; font-size: 14px; color: #f56c6c">Risks:</h4>
                <ul style="margin: 0; padding-left: 20px">
                  <li v-for="r in emailResult.risks" :key="r" style="color: #f56c6c; font-size: 13px">{{ r }}</li>
                </ul>
              </div>
            </el-card>
            <el-empty v-else description="Paste a customer email reply and click Analyze" />
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 3: Batch Email Generator -->
      <el-tab-pane label="Batch Email Generator" name="batch">
        <el-row :gutter="20">
          <el-col :span="10">
            <el-card shadow="never">
              <template #header><span style="font-weight: 600">Batch Personalized Emails</span></template>
              <el-form label-width="120px">
                <el-form-item label="Product" required>
                  <el-input v-model="batchForm.product_name" placeholder="e.g. LED Panel Light 60x60cm" />
                </el-form-item>
                <el-form-item label="Your Company">
                  <el-input v-model="batchForm.company_name" />
                </el-form-item>
                <el-form-item label="Selling Points">
                  <el-input v-model="batchForm.selling_points" type="textarea" :rows="2" />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" @click="generateBatchEmails" :loading="batchLoading" size="large">
                    <el-icon><MagicStick /></el-icon> Generate for All Customers
                  </el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="14">
            <el-card v-if="batchResult" shadow="never">
              <template #header>
                <div style="display: flex; justify-content: space-between; align-items: center">
                  <span style="font-weight: 600">Generated Emails ({{ batchResult.generated }}/{{ batchResult.total }})</span>
                </div>
              </template>
              <div v-for="e in (batchResult.emails || [])" :key="e.customer_id"
                style="border: 1px solid #e4e7ed; border-radius: 8px; padding: 12px; margin-bottom: 12px">
                <div style="display: flex; justify-content: space-between; margin-bottom: 8px">
                  <strong>{{ e.company_name }}</strong>
                  <el-tag size="small" type="info">{{ e.personalized_for || e.error }}</el-tag>
                </div>
                <p style="margin: 0 0 4px; font-weight: 600; font-size: 13px" v-if="e.subject">Subject: {{ e.subject }}</p>
                <div v-if="e.body" v-html="e.body" style="background: #f5f7fa; padding: 12px; border-radius: 6px; font-size: 13px; color: #303133"></div>
                <el-button v-if="e.body && e.to_email" size="small" type="success" style="margin-top: 8px"
                  @click="sendBatchEmail(e)">Send to {{ e.to_email }}</el-button>
              </div>
            </el-card>
            <el-empty v-else description="Enter your product info and click Generate" />
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 4: Negotiation Copilot -->
      <el-tab-pane label="Negotiation Copilot" name="negotiation">
        <el-row :gutter="20">
          <el-col :span="10">
            <el-card shadow="never">
              <template #header><span style="font-weight: 600">Negotiation Input</span></template>
              <el-form label-width="120px">
                <el-form-item label="Customer Said" required>
                  <el-input v-model="negoForm.customer_message" type="textarea" :rows="4"
                    placeholder='e.g. "Your price is too high. We can get $5.5/unit from your competitor. Can you match that?"' />
                </el-form-item>
                <el-form-item label="Product" required>
                  <el-input v-model="negoForm.product_name" placeholder="e.g. LED Panel Light 60x60cm" />
                </el-form-item>
                <el-form-item label="Your Cost ($)">
                  <el-input-number v-model="negoForm.your_cost" :min="0" :precision="2" style="width: 100%" />
                </el-form-item>
                <el-form-item label="Your Quote ($)">
                  <el-input-number v-model="negoForm.your_quote" :min="0" :precision="2" style="width: 100%" />
                </el-form-item>
                <el-form-item label="Context">
                  <el-input v-model="negoForm.context" type="textarea" :rows="2"
                    placeholder="Additional context (optional)" />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" @click="getAdvice" :loading="negoLoading" size="large">
                    <el-icon><MagicStick /></el-icon> Get Strategies
                  </el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="14">
            <el-card v-if="negoResult" shadow="never">
              <template #header>
                <div style="display: flex; justify-content: space-between; align-items: center">
                  <span style="font-weight: 600">Negotiation Strategies</span>
                  <el-tag :type="negoResult.ai_powered ? 'success' : 'info'" size="small">
                    {{ negoResult.ai_powered ? 'AI Powered' : 'Basic' }}
                  </el-tag>
                </div>
              </template>

              <!-- Customer Intent -->
              <el-alert v-if="negoResult.customer_intent_analysis"
                :title="'Customer Intent: ' + negoResult.customer_intent_analysis"
                type="warning" :closable="false" show-icon style="margin-bottom: 16px" />

              <!-- Strategies -->
              <div v-for="(s, i) in (negoResult.strategies || [])" :key="i"
                style="border: 1px solid #e4e7ed; border-radius: 8px; padding: 16px; margin-bottom: 12px"
                :style="{ borderColor: i === 0 ? '#67c23a' : '#e4e7ed' }">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px">
                  <el-tag :type="i === 0 ? 'success' : (i === 1 ? 'warning' : 'info')" size="small">
                    Strategy {{ i + 1 }}: {{ s.type || s.name }}
                  </el-tag>
                  <span v-if="i === 0 && negoResult.recommended_strategy" style="color: #67c23a; font-size: 12px; font-weight: 600">RECOMMENDED</span>
                </div>
                <p v-if="s.name" style="margin: 0 0 8px; font-weight: 600; font-size: 14px">{{ s.name }}</p>
                <div style="background: #f5f7fa; padding: 12px; border-radius: 6px; margin-bottom: 8px">
                  <p style="margin: 0; color: #303133; font-size: 13px; white-space: pre-wrap">{{ s.reply }}</p>
                </div>
                <div style="display: flex; gap: 20px; font-size: 12px">
                  <span v-if="s.pros" style="color: #67c23a">Pros: {{ s.pros }}</span>
                  <span v-if="s.cons" style="color: #f56c6c">Cons: {{ s.cons }}</span>
                </div>
              </div>

              <!-- Talking Points -->
              <div v-if="negoResult.talking_points && negoResult.talking_points.length" style="margin-top: 16px">
                <h4 style="margin: 0 0 8px; font-size: 14px">Talking Points:</h4>
                <ul style="margin: 0; padding-left: 20px">
                  <li v-for="t in negoResult.talking_points" :key="t" style="color: #606266; font-size: 13px; margin-bottom: 4px">{{ t }}</li>
                </ul>
              </div>

              <!-- Red Flags -->
              <div v-if="negoResult.red_flags && negoResult.red_flags.length" style="margin-top: 12px">
                <h4 style="margin: 0 0 8px; font-size: 14px; color: #f56c6c">Red Flags:</h4>
                <ul style="margin: 0; padding-left: 20px">
                  <li v-for="r in negoResult.red_flags" :key="r" style="color: #f56c6c; font-size: 13px; margin-bottom: 4px">{{ r }}</li>
                </ul>
              </div>
            </el-card>
            <el-empty v-else description="Enter the customer's message and click Get Strategies" />
          </el-col>
        </el-row>
      </el-tab-pane>

    </el-tabs>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { agentApi } from '../api'

const activeTab = ref('daily')

// Daily Intelligence
const dailyLoading = ref(false)
const dailyResult = ref(null)

const runDailyIntelligence = async () => {
  dailyLoading.value = true
  try {
    dailyResult.value = await agentApi.dailyIntelligence()
  } catch (e) {
    ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    dailyLoading.value = false
  }
}

const showDraftEmail = (action) => {
  action.showDraft = !action.showDraft
}

const sendQuickEmail = async (action) => {
  try {
    const { emailApi } = await import('../api')
    await emailApi.send({
      to_email: action.contact_email,
      to_name: action.contact_name,
      subject: action.draft_subject || `Following up - ${action.company_name}`,
      body: action.draft_email || 'Following up on our previous conversation.',
      customer_id: action.customer_id,
    })
    ElMessage.success(`Email sent to ${action.contact_email}`)
  } catch (e) {
    ElMessage.error('Failed to send email')
  }
}

// Batch Email
const batchLoading = ref(false)
const batchResult = ref(null)
const batchForm = reactive({ product_name: '', company_name: '', selling_points: '' })

const generateBatchEmails = async () => {
  if (!batchForm.product_name.trim()) { ElMessage.warning('Product name is required'); return }
  batchLoading.value = true
  try {
    batchResult.value = await agentApi.batchEmails(batchForm)
  } catch (e) {
    ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    batchLoading.value = false
  }
}

const sendBatchEmail = async (e) => {
  try {
    const { emailApi } = await import('../api')
    await emailApi.send({
      to_email: e.to_email,
      to_name: e.contact_name,
      subject: e.subject,
      body: e.body,
      customer_id: e.customer_id,
    })
    ElMessage.success(`Email sent to ${e.to_email}`)
  } catch (err) {
    ElMessage.error('Failed to send email')
  }
}


// Website Analysis
const webLoading = ref(false)
const webResult = ref(null)
const webForm = reactive({ url: '', your_products: '' })

const analyzeWebsite = async () => {
  if (!webForm.url.trim()) { ElMessage.warning('Please enter a URL'); return }
  webLoading.value = true
  try {
    webResult.value = await agentApi.analyzeWebsite(webForm)
  } catch (e) {
    ElMessage.error('Analysis failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    webLoading.value = false
  }
}

// Email Analysis
const emailLoading = ref(false)
const emailResult = ref(null)
const emailForm = reactive({ email_content: '' })

const analyzeEmail = async () => {
  if (!emailForm.email_content.trim()) { ElMessage.warning('Please paste email content'); return }
  emailLoading.value = true
  try {
    emailResult.value = await agentApi.analyzeEmail(emailForm)
  } catch (e) {
    ElMessage.error('Analysis failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    emailLoading.value = false
  }
}

// Negotiation
const negoLoading = ref(false)
const negoResult = ref(null)
const negoForm = reactive({ customer_message: '', product_name: '', your_cost: 0, your_quote: 0, context: '' })

const getAdvice = async () => {
  if (!negoForm.customer_message.trim() || !negoForm.product_name.trim()) {
    ElMessage.warning('Please fill in customer message and product'); return
  }
  negoLoading.value = true
  try {
    negoResult.value = await agentApi.negotiationAdvice(negoForm)
  } catch (e) {
    ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message))
  } finally {
    negoLoading.value = false
  }
}

// Helpers
const scoreColor = (score) => {
  if (score >= 70) return '#67c23a'
  if (score >= 40) return '#e6a23c'
  return '#f56c6c'
}
const intentColor = (i) => ({ inquiry: 'success', price_negotiation: 'warning', sample_request: '', rejection: 'danger', order: 'success', follow_up: 'info' }[i] || '')
const sentimentColor = (s) => ({ positive: 'success', neutral: '', negative: 'danger' }[s] || '')
const urgencyColor = (u) => ({ high: 'danger', medium: 'warning', low: 'info' }[u] || '')
</script>