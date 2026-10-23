import { createRouter, createWebHistory } from 'vue-router'
import { t } from '../i18n/index.js'

const routes = [
  {
    // The agent is the front door now. The dashboard is a supporting page.
    path: '/',
    redirect: '/console',
  },
  {
    path: '/console',
    name: 'Console',
    component: () => import('../views/Console.vue'),
    meta: { titleKey: 'console.title', icon: 'ChatDotRound' },
  },
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: () => import('../views/Dashboard.vue'),
    meta: { titleKey: 'dashboard.title', icon: 'DataBoard' },
  },
  {
    path: '/customers',
    name: 'Customers',
    component: () => import('../views/Customers.vue'),
    meta: { titleKey: 'customers.title', icon: 'User' },
  },
  {
    path: '/customers/:id',
    name: 'CustomerDetail',
    component: () => import('../views/CustomerDetail.vue'),
    meta: { titleKey: 'customerDetail.title', hidden: true },
  },
  {
    path: '/emails',
    name: 'Emails',
    component: () => import('../views/Emails.vue'),
    meta: { titleKey: 'emails.title', icon: 'Message' },
  },
  {
    path: '/templates',
    name: 'Templates',
    component: () => import('../views/Templates.vue'),
    meta: { titleKey: 'templates.title', icon: 'Document' },
  },
  {
    path: '/tasks',
    name: 'Tasks',
    component: () => import('../views/Tasks.vue'),
    meta: { titleKey: 'tasks.title', icon: 'Calendar' },
  },
  {
    path: '/agent',
    name: 'Agent',
    component: () => import('../views/Agent.vue'),
    meta: { titleKey: 'agent.title', icon: 'Tools' },
  },
  {
    path: '/runs',
    name: 'Runs',
    component: () => import('../views/Runs.vue'),
    meta: { titleKey: 'runs.title', icon: 'Guide' },
  },
  {
    path: '/quality',
    name: 'Quality',
    component: () => import('../views/Quality.vue'),
    meta: { titleKey: 'quality.title', icon: 'TrendCharts' },
  },
  {
    // Operator configuration, not business data: which mailbox a send leaves
    // from. It sits after the agent group because nothing above it depends
    // on it, and everything sending depends on it.
    path: '/senders',
    name: 'Senders',
    component: () => import('../views/Senders.vue'),
    meta: { titleKey: 'senders.title', icon: 'Setting' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to, from, next) => {
  const label = to.meta.titleKey ? t(to.meta.titleKey) : 'TradeAgent'
  document.title = `${label} - TradeAgent`
  next()
})

export default router
