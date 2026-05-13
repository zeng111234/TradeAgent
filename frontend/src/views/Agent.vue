<template>
  <div>
    <div class="page-header">
      <h2>AI Agent</h2>
      <p>AI-powered tools for smarter foreign trade decisions</p>
    </div>

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
                      <el-button v-if="a.draft_email" size="small" type="success" @click="showDraftEmail(a)">View Draft</el-button>
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

      <!-- Tab 1: Inquiry Analysis -->
      <el-tab-pane label="Inquiry Analysis" name="inquiry">
        <el-row :gutter="20">
          <el-col :span="10">
            <el-card shadow="never">
              <template #header><span style="font-weight: 600">Paste Customer Inquiry</span></template>
              <el-form label-width="100px">
                <el-form-item label="Inquiry Email" required>
                  <el-input v-model="inquiryForm.email_content" type="textarea" :rows="12"
                    placeholder="Paste the customer's inquiry email here..." />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" @click="analyzeInquiry" :loading="inquiryLoading" size="large">
                    <el-icon><MagicStick /></el-icon> Parse Inquiry
                  </el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="14">
            <el-card v-if="inquiryResult" shadow="never">
              <template #header>
                <div style="display: flex; justify-content: space-between; align-items: center">
                  <span style="font-weight: 600">Inquiry Intelligence Report</span>
                  <el-tag :type="inquiryResult.ai_powered ? 'success' : 'info'" size="small">
                    {{ inquiryResult.ai_powered ? 'AI Powered' : 'Basic' }}
                  </el-tag>
                </div>
              </template>
              <el-row :gutter="16" style="margin-bottom: 16px">
                <el-col :span="8">
                  <div style="text-align: center; padding: 12px; background: #f5f7fa; border-radius: 8px">
                    <el-tag size="large" type="primary">{{ inquiryResult.inquiry_type }}</el-tag>
                    <p style="margin-top: 4px; font-size: 12px; color: #909399">Type</p>
                  </div>
                </el-col>
                <el-col :span="8">
                  <div style="text-align: center; padding: 12px; background: #f5f7fa; border-radius: 8px">
                    <el-tag :type="urgencyColor(inquiryResult.urgency)" size="large">{{ inquiryResult.urgency }}</el-tag>
                    <p style="margin-top: 4px; font-size: 12px; color: #909399">Urgency</p>
                  </div>
                </el-col>
                <el-col :span="8">
                  <div style="text-align: center; padding: 12px; background: #f5f7fa; border-radius: 8px">
                    <el-progress type="dashboard" :percentage="inquiryResult.customer_intent_score || 0"
                      :color="scoreColor(inquiryResult.customer_intent_score || 0)" :width="60" />
                    <p style="margin-top: 4px; font-size: 12px; color: #909399">Intent Score</p>
                  </div>
                </el-col>
              </el-row>
              <el-descriptions v-if="inquiryResult.customer_info" :column="2" border size="small" style="margin-bottom: 16px">
                <el-descriptions-item label="Name">{{ inquiryResult.customer_info.name }}</el-descriptions-item>
                <el-descriptions-item label="Company">{{ inquiryResult.customer_info.company }}</el-descriptions-item>
                <el-descriptions-item label="Country">{{ inquiryResult.customer_info.country }}</el-descriptions-item>
                <el-descriptions-item label="Source">{{ inquiryResult.customer_info.contact_method }}</el-descriptions-item>
              </el-descriptions>
              <div v-if="inquiryResult.products && inquiryResult.products.length" style="margin-bottom: 16px">
                <h4 style="margin: 0 0 8px; font-size: 14px">Products Requested:</h4>
                <div v-for="(p, i) in inquiryResult.products" :key="i"
                  style="background: #f5f7fa; padding: 10px; border-radius: 6px; margin-bottom: 6px">
                  <strong>{{ p.name }}</strong>
                  <span v-if="p.specification" style="color: #909399"> | {{ p.specification }}</span>
                  <span v-if="p.quantity" style="color: #409eff; margin-left: 8px">Qty: {{ p.quantity }}</span>
                  <span v-if="p.target_price" style="color: #e6a23c; margin-left: 8px">Target: {{ p.target_price }}</span>
                </div>
              </div>
              <el-descriptions v-if="inquiryResult.delivery_requirements" :column="2" border size="small" style="margin-bottom: 16px">
                <el-descriptions-item label="Deadline">{{ inquiryResult.delivery_requirements.deadline }}</el-descriptions-item>
                <el-descriptions-item label="Shipping">{{ inquiryResult.delivery_requirements.shipping_terms }}</el-descriptions-item>
                <el-descriptions-item label="Port" :span="2">{{ inquiryResult.delivery_requirements.destination_port }}</el-descriptions-item>
              </el-descriptions>
              <div v-if="inquiryResult.key_concerns && inquiryResult.key_concerns.length" style="margin-bottom: 12px">
                <h4 style="margin: 0 0 6px; font-size: 14px">Customer Concerns:</h4>
                <el-tag v-for="c in inquiryResult.key_concerns" :key="c" type="warning" style="margin: 2px 4px">{{ c }}</el-tag>
              </div>
              <div v-if="inquiryResult.missing_info && inquiryResult.missing_info.length" style="margin-bottom: 16px">
                <h4 style="margin: 0 0 6px; font-size: 14px; color: #f56c6c">Missing Info (Ask For):</h4>
                <el-tag v-for="m in inquiryResult.missing_info" :key="m" type="danger" style="margin: 2px 4px">{{ m }}</el-tag>
              </div>
              <el-card v-if="inquiryResult.suggested_reply_draft" shadow="never" style="background: #ecf5ff; border-color: #b3d8ff">
                <template #header>
                  <div style="display: flex; justify-content: space-between; align-items: center">
                    <span style="font-weight: 600">AI Draft Reply</span>
                    <el-button size="small" type="primary" @click="useInquiryReply">Use This Reply</el-button>
                  </div>
                </template>
                <p style="margin: 0 0 4px; font-weight: 600; font-size: 13px">Subject: {{ inquiryResult.suggested_reply_subject }}</p>
                <div v-html="inquiryResult.suggested_reply_draft" style="font-size: 13px; color: #303133"></div>
              </el-card>
              <el-alert v-if="inquiryResult.follow_up_strategy"
                :title="'Follow-up: ' + inquiryResult.follow_up_strategy"
                type="info" :closable="false" show-icon style="margin-top: 12px" />
            </el-card>
            <el-empty v-else description="Paste a customer inquiry and click Parse" />
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 2: Churn Alerts -->
      <el-tab-pane label="Churn Alerts" name="churn">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span style="font-weight: 600">Customer Churn Risk Scanner</span>
              <el-button type="danger" @click="runChurnScan" :loading="churnLoading" size="large">
                <el-icon><WarningFilled /></el-icon> Scan for At-Risk Customers
              </el-button>
            </div>
          </template>
          <div v-if="churnResult" style="margin-bottom: 16px">
            <el-row :gutter="16">
              <el-col :span="6"><el-statistic title="Total Alerts" :value="churnResult.total_alerts" /></el-col>
              <el-col :span="6"><el-statistic title="Critical" :value="churnResult.critical" /></el-col>
              <el-col :span="6"><el-statistic title="High Risk" :value="churnResult.high" /></el-col>
              <el-col :span="6"><el-statistic title="Medium Risk" :value="churnResult.medium" /></el-col>
            </el-row>
          </div>
          <div v-if="churnResult && churnResult.alerts && churnResult.alerts.length">
            <div v-for="a in churnResult.alerts" :key="a.customer_id"
              style="border: 1px solid #e4e7ed; border-radius: 8px; padding: 16px; margin-bottom: 12px"
              :style="{ borderColor: riskColor(a.risk_level), borderLeftWidth: '4px' }">
              <el-row :gutter="16">
                <el-col :span="14">
                  <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px">
                    <el-tag :type="riskTagType(a.risk_level)" size="small">{{ a.risk_level }}</el-tag>
                    <strong>{{ a.company_name }}</strong>
                    <el-tag size="small" type="info">{{ a.stage }}</el-tag>
                    <span v-if="a.country" style="color: #909399; font-size: 12px">{{ a.country }}</span>
                  </div>
                  <div v-for="(reason, i) in a.risk_reasons" :key="i">
                    <p style="margin: 0 0 2px; color: #f56c6c; font-size: 13px">{{ reason }}</p>
                  </div>
                  <div style="margin-top: 8px">
                    <span style="font-size: 12px; color: #909399">Suggested: </span>
                    <span v-for="(action, i) in a.suggested_actions" :key="i" style="color: #409eff; font-size: 12px">
                      {{ action }}{{ i < a.suggested_actions.length - 1 ? ' | ' : '' }}
                    </span>
                  </div>
                  <div style="margin-top: 6px; font-size: 11px; color: #909399">
                    Sent: {{ a.emails_sent }} | Replied: {{ a.emails_replied }} | Days in stage: {{ a.days_in_stage }}
                  </div>
                </el-col>
                <el-col :span="10" style="text-align: right">
                  <el-button v-if="a.rescue_email" size="small" type="success" @click="a.showRescue = !a.showRescue">Rescue Email</el-button>
                  <el-button v-if="a.contact_email" size="small" type="primary" @click="sendRescueEmail(a)">Send Now</el-button>
                </el-col>
              </el-row>
              <div v-if="a.showRescue && a.rescue_email" style="background: #ecf5ff; padding: 12px; border-radius: 6px; margin-top: 12px">
                <p style="margin: 0 0 4px; font-weight: 600; font-size: 13px">Subject: {{ a.rescue_subject }}</p>
                <div v-html="a.rescue_email" style="font-size: 13px; color: #303133"></div>
              </div>
            </div>
          </div>
          <el-empty v-else-if="churnResult && !churnResult.alerts?.length" description="No at-risk customers found!" />
          <el-empty v-else description="Click 'Scan' to check all customers for churn risk" />
        </el-card>
      </el-tab-pane>

      <!-- Tab 3: Holiday Emails -->
      <el-tab-pane label="Holiday Emails" name="holiday">
        <el-row :gutter="20">
          <el-col :span="10">
            <el-card shadow="never">
              <template #header><span style="font-weight: 600">Holiday Greeting Generator</span></template>
              <el-alert title="Select a holiday and AI will generate personalized greetings for ALL your customers" type="info" :closable="false" style="margin-bottom: 16px" />
              <el-form label-width="120px">
                <el-form-item label="Holiday" required>
                  <el-select v-model="holidayForm.holiday_name" placeholder="Select a holiday" style="width: 100%">
                    <el-option v-for="h in holidays" :key="h.name" :label="`${h.name} (${h.days_until} days)`" :value="h.name">
                      <span>{{ h.name }}</span>
                      <el-tag v-if="h.upcoming" type="warning" size="small" style="margin-left: 8px">Upcoming!</el-tag>
                    </el-option>
                  </el-select>
                </el-form-item>
                <el-form-item label="Your Company">
                  <el-input v-model="holidayForm.company_name" placeholder="Your company name" />
                </el-form-item>
                <el-form-item label="Custom Message">
                  <el-input v-model="holidayForm.custom_message" type="textarea" :rows="2"
                    placeholder="Extra message to include (optional)" />
                </el-form-item>
                <el-form-item>
                  <el-button type="primary" @click="generateHolidayEmails" :loading="holidayLoading" size="large">
                    <el-icon><MagicStick /></el-icon> Generate Holiday Emails
                  </el-button>
                </el-form-item>
              </el-form>
            </el-card>
          </el-col>
          <el-col :span="14">
            <el-card v-if="holidayResult" shadow="never">
              <template #header>
                <div style="display: flex; justify-content: space-between; align-items: center">
                  <span style="font-weight: 600">{{ holidayResult.holiday }} Emails ({{ holidayResult.generated }})</span>
                  <el-button type="success" size="small" @click="sendAllHolidayEmails">Send All</el-button>
                </div>
              </template>
              <div v-for="e in (holidayResult.emails || [])" :key="e.customer_id"
                style="border: 1px solid #e4e7ed; border-radius: 8px; padding: 12px; margin-bottom: 12px">
                <div style="display: flex; justify-content: space-between; margin-bottom: 8px">
                  <strong>{{ e.company_name }} - {{ e.contact_name }}</strong>
                  <el-tag size="small" :type="e.personalized ? 'success' : 'info'">
                    {{ e.personalized ? 'AI Personalized' : 'Template' }}
                  </el-tag>
                </div>
                <p style="margin: 0 0 4px; font-weight: 600; font-size: 13px">Subject: {{ e.subject }}</p>
                <div v-html="e.body" style="background: #f5f7fa; padding: 12px; border-radius: 6px; font-size: 13px; color: #303133"></div>
                <el-button size="small" type="success" style="margin-top: 8px"
                  @click="sendSingleHolidayEmail(e)">Send to {{ e.to_email }}</el-button>
              </div>
            </el-card>
            <el-empty v-else description="Select a holiday and click Generate" />
          </el-col>
        </el-row>
      </el-tab-pane>

      <!-- Tab 4: Website Analysis -->
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

      <!-- Tab 5: Email Reply Analysis -->
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
              <el-row :gutter="16" style="margin-bottom: 20px">
                <el-col :span="8">
                  <div style="text-align: center; padding: 12px">
                    <el-tag :type="intentColor(emailResult.intent)" size="large">{{ emailResult.intent }}</el-tag>
                    <p style="margin-top: 4px; font-size: 12px; color: #909399">Intent</p>
                  </div>
                </el-col>
                <el-col :span="8">
                  <div style="text-align: center; padding: 12px">
                    <el-tag :type="sentimentColor(emailResult.sentiment)" size="large">{{ emailResult.sentiment }}</el-tag>
                    <p style="margin-top: 4px; font-size: 12px; color: #909399">Sentiment</p>
                  </div>
                </el-col>
                <el-col :span="8">
                  <div style="text-align: center; padding: 12px">
                    <el-tag :type="urgencyColor(emailResult.urgency)" size="large">{{ emailResult.urgency }}</el-tag>
                    <p style="margin-top: 4px; font-size: 12px; color: #909399">Urgency</p>
                  </div>
                </el-col>
              </el-row>
              <el-descriptions v-if="emailResult.extracted_info" :column="2" border size="small" style="margin-bottom: 16px">
                <el-descriptions-item label="Products" :span="2">
                  {{ (emailResult.extracted_info.products_mentioned || []).join(', ') || '-' }}
                </el-descriptions-item>
                <el-descriptions-item label="Quantity">{{ emailResult.extracted_info.quantity_mentioned || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Budget">{{ emailResult.extracted_info.budget_mentioned || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Deadline">{{ emailResult.extracted_info.delivery_deadline || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Target Price">{{ emailResult.extracted_info.target_price || '-' }}</el-descriptions-item>
              </el-descriptions>
              <el-alert :title="'Recommended: ' + emailResult.recommended_action" type="success" :closable="false" show-icon style="margin-bottom: 16px" />
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

      <!-- Tab 6: Batch Email Generator -->
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
                <span style="font-weight: 600">Generated Emails ({{ batchResult.generated }}/{{ batchResult.total }})</span>
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

      <!-- Tab 7: Negotiation Copilot -->
      <el-tab-pane label="Negotiation Copilot" name="negotiation">
        <el-row :gutter="20">
          <el-col :span="10">
            <el-card shadow="never">
              <template #header><span style="font-weight: 600">Negotiation Input</span></template>
              <el-form label-width="120px">
                <el-form-item label="Customer Said" required>
                  <el-input v-model="negoForm.customer_message" type="textarea" :rows="4"
                    placeholder='e.g. "Your price is too high..."' />
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
                  <el-input v-model="negoForm.context" type="textarea" :rows="2" placeholder="Additional context (optional)" />
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
              <el-alert v-if="negoResult.customer_intent_analysis"
                :title="'Customer Intent: ' + negoResult.customer_intent_analysis"
                type="warning" :closable="false" show-icon style="margin-bottom: 16px" />
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
              <div v-if="negoResult.talking_points && negoResult.talking_points.length" style="margin-top: 16px">
                <h4 style="margin: 0 0 8px; font-size: 14px">Talking Points:</h4>
                <ul style="margin: 0; padding-left: 20px">
                  <li v-for="t in negoResult.talking_points" :key="t" style="color: #606266; font-size: 13px; margin-bottom: 4px">{{ t }}</li>
                </ul>
              </div>
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
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { agentApi, emailApi } from '../api'

const activeTab = ref('daily')

// Daily Intelligence
const dailyLoading = ref(false)
const dailyResult = ref(null)
const runDailyIntelligence = async () => {
  dailyLoading.value = true
  try { dailyResult.value = await agentApi.dailyIntelligence() }
  catch (e) { ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message)) }
  finally { dailyLoading.value = false }
}
const showDraftEmail = (a) => { a.showDraft = !a.showDraft }
const sendQuickEmail = async (a) => {
  try {
    await emailApi.send({ to_email: a.contact_email, to_name: a.contact_name, subject: a.draft_subject || `Following up - ${a.company_name}`, body: a.draft_email || 'Following up.', customer_id: a.customer_id })
    ElMessage.success(`Email sent to ${a.contact_email}`)
  } catch (e) { ElMessage.error('Failed to send email') }
}

// Inquiry Analysis
const inquiryLoading = ref(false)
const inquiryResult = ref(null)
const inquiryForm = reactive({ email_content: '' })
const analyzeInquiry = async () => {
  if (!inquiryForm.email_content.trim()) { ElMessage.warning('Please paste inquiry content'); return }
  inquiryLoading.value = true
  try { inquiryResult.value = await agentApi.analyzeInquiry(inquiryForm) }
  catch (e) { ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message)) }
  finally { inquiryLoading.value = false }
}
const useInquiryReply = () => {
  ElMessage.success('Reply draft copied - navigate to Emails to send it')
}

// Churn Alerts
const churnLoading = ref(false)
const churnResult = ref(null)
const runChurnScan = async () => {
  churnLoading.value = true
  try { churnResult.value = await agentApi.churnAlerts() }
  catch (e) { ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message)) }
  finally { churnLoading.value = false }
}
const sendRescueEmail = async (a) => {
  try {
    await emailApi.send({ to_email: a.contact_email, to_name: a.contact_name, subject: a.rescue_subject || `Reconnect - ${a.company_name}`, body: a.rescue_email || 'Just checking in.', customer_id: a.customer_id })
    ElMessage.success(`Rescue email sent to ${a.contact_email}`)
  } catch (e) { ElMessage.error('Failed to send') }
}
const riskColor = (level) => ({ critical: '#f56c6c', high: '#e6a23c', medium: '#909399' }[level] || '#e4e7ed')
const riskTagType = (level) => ({ critical: 'danger', high: 'warning', medium: 'info' }[level] || '')

// Holiday Emails
const holidayLoading = ref(false)
const holidayResult = ref(null)
const holidays = ref([])
const holidayForm = reactive({ holiday_name: '', company_name: '', custom_message: '' })
onMounted(async () => {
  try { holidays.value = await agentApi.getHolidays() } catch (e) { /* ignore */ }
})
const generateHolidayEmails = async () => {
  if (!holidayForm.holiday_name) { ElMessage.warning('Please select a holiday'); return }
  holidayLoading.value = true
  try { holidayResult.value = await agentApi.generateHolidayEmails(holidayForm) }
  catch (e) { ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message)) }
  finally { holidayLoading.value = false }
}
const sendSingleHolidayEmail = async (e) => {
  try {
    await emailApi.send({ to_email: e.to_email, to_name: e.contact_name, subject: e.subject, body: e.body, customer_id: e.customer_id })
    ElMessage.success(`Sent to ${e.to_email}`)
  } catch (err) { ElMessage.error('Failed to send') }
}
const sendAllHolidayEmails = async () => {
  if (!holidayResult.value?.emails) return
  let sent = 0
  for (const e of holidayResult.value.emails) {
    try {
      await emailApi.send({ to_email: e.to_email, to_name: e.contact_name, subject: e.subject, body: e.body, customer_id: e.customer_id })
      sent++
    } catch (err) { /* skip failed */ }
  }
  ElMessage.success(`Sent ${sent} holiday emails!`)
}

// Website Analysis
const webLoading = ref(false)
const webResult = ref(null)
const webForm = reactive({ url: '', your_products: '' })
const analyzeWebsite = async () => {
  if (!webForm.url.trim()) { ElMessage.warning('Please enter a URL'); return }
  webLoading.value = true
  try { webResult.value = await agentApi.analyzeWebsite(webForm) }
  catch (e) { ElMessage.error('Analysis failed: ' + (e.response?.data?.detail || e.message)) }
  finally { webLoading.value = false }
}

// Email Analysis
const emailLoading = ref(false)
const emailResult = ref(null)
const emailForm = reactive({ email_content: '' })
const analyzeEmail = async () => {
  if (!emailForm.email_content.trim()) { ElMessage.warning('Please paste email content'); return }
  emailLoading.value = true
  try { emailResult.value = await agentApi.analyzeEmail(emailForm) }
  catch (e) { ElMessage.error('Analysis failed: ' + (e.response?.data?.detail || e.message)) }
  finally { emailLoading.value = false }
}

// Batch Email
const batchLoading = ref(false)
const batchResult = ref(null)
const batchForm = reactive({ product_name: '', company_name: '', selling_points: '' })
const generateBatchEmails = async () => {
  if (!batchForm.product_name.trim()) { ElMessage.warning('Product name is required'); return }
  batchLoading.value = true
  try { batchResult.value = await agentApi.batchEmails(batchForm) }
  catch (e) { ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message)) }
  finally { batchLoading.value = false }
}
const sendBatchEmail = async (e) => {
  try {
    await emailApi.send({ to_email: e.to_email, to_name: e.contact_name, subject: e.subject, body: e.body, customer_id: e.customer_id })
    ElMessage.success(`Sent to ${e.to_email}`)
  } catch (err) { ElMessage.error('Failed to send') }
}

// Negotiation
const negoLoading = ref(false)
const negoResult = ref(null)
const negoForm = reactive({ customer_message: '', product_name: '', your_cost: 0, your_quote: 0, context: '' })
const getAdvice = async () => {
  if (!negoForm.customer_message.trim() || !negoForm.product_name.trim()) { ElMessage.warning('Please fill in required fields'); return }
  negoLoading.value = true
  try { negoResult.value = await agentApi.negotiationAdvice(negoForm) }
  catch (e) { ElMessage.error('Failed: ' + (e.response?.data?.detail || e.message)) }
  finally { negoLoading.value = false }
}

// Helpers
const scoreColor = (s) => s >= 70 ? '#67c23a' : s >= 40 ? '#e6a23c' : '#f56c6c'
const intentColor = (i) => ({ inquiry: 'success', price_negotiation: 'warning', sample_request: '', rejection: 'danger', order: 'success', follow_up: 'info' }[i] || '')
const sentimentColor = (s) => ({ positive: 'success', neutral: '', negative: 'danger' }[s] || '')
const urgencyColor = (u) => ({ high: 'danger', medium: 'warning', low: 'info' }[u] || '')
</script>