<template>
  <div>
    <div class="page-header">
      <h2>AI Agent</h2>
      <p>AI-powered tools for smarter foreign trade decisions</p>
    </div>

    <!-- Three Tools Tabs -->
    <el-tabs v-model="activeTab" type="border-card" style="margin-bottom: 20px">

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

      <!-- Tab 3: Negotiation Copilot -->
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

const activeTab = ref('website')

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