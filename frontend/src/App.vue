<template>
  <!-- Element Plus is installed once with a fixed locale, so its own strings
       (a table's "暂无数据"/"No Data", pagination, the date picker) would
       freeze at whatever that was. A provider follows the language switcher
       instead. It renders no DOM of its own, so the layout is untouched. -->
  <el-config-provider :locale="epLocale">
  <div class="layout">
    <!-- Sidebar: the agent is the product, so it comes first; business data
         is context underneath. -->
    <aside class="sidebar">
      <div class="sidebar-header">
        <h1>TradeAgent</h1>
        <p>{{ t('shell.tagline') }}</p>
      </div>

      <el-menu
        :default-active="currentRoute"
        :router="true"
        background-color="transparent"
        text-color="rgba(255,255,255,0.65)"
        active-text-color="#fff"
      >
        <el-menu-item-group>
          <template #title><span class="nav-group">{{ t('shell.groupAgent') }}</span></template>
          <el-menu-item index="/console">
            <el-icon><ChatDotRound /></el-icon>
            <span>{{ t('nav.console') }}</span>
          </el-menu-item>
          <el-menu-item index="/agent">
            <el-icon><Tools /></el-icon>
            <span>{{ t('nav.agent') }}</span>
          </el-menu-item>
          <el-menu-item index="/runs">
            <el-icon><Guide /></el-icon>
            <span>{{ t('nav.runs') }}</span>
          </el-menu-item>
          <el-menu-item index="/quality">
            <el-icon><TrendCharts /></el-icon>
            <span>{{ t('nav.quality') }}</span>
          </el-menu-item>
        </el-menu-item-group>

        <el-menu-item-group>
          <template #title><span class="nav-group">{{ t('shell.groupData') }}</span></template>
          <el-menu-item index="/dashboard">
            <el-icon><DataBoard /></el-icon>
            <span>{{ t('nav.dashboard') }}</span>
          </el-menu-item>
          <el-menu-item index="/customers">
            <el-icon><User /></el-icon>
            <span>{{ t('nav.customers') }}</span>
          </el-menu-item>
          <el-menu-item index="/emails">
            <el-icon><Message /></el-icon>
            <span>{{ t('nav.emails') }}</span>
          </el-menu-item>
          <el-menu-item index="/templates">
            <el-icon><Document /></el-icon>
            <span>{{ t('nav.templates') }}</span>
          </el-menu-item>
          <el-menu-item index="/tasks">
            <el-icon><Calendar /></el-icon>
            <span>{{ t('nav.tasks') }}</span>
          </el-menu-item>
        </el-menu-item-group>

        <el-menu-item-group>
          <template #title><span class="nav-group">{{ t('shell.groupSystem') }}</span></template>
          <el-menu-item index="/senders">
            <el-icon><Setting /></el-icon>
            <span>{{ t('nav.senders') }}</span>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>

      <div class="sidebar-footer">
        <p>TradeAgent v1.0</p>
        <p>{{ t('shell.copyright') }}</p>
      </div>
    </aside>

    <!-- Main column: slim topbar with the global agent command, then the view -->
    <div class="shell-main">
      <header class="topbar">
        <div class="topbar-crumb">
          <span class="crumb-agent">{{ t('shell.groupAgent') }}</span>
          <span class="crumb-sep">/</span>
          <span class="crumb-page">{{ pageLabel }}</span>
        </div>

        <form class="topbar-command" @submit.prevent="submitCommand">
          <el-icon class="command-icon"><Promotion /></el-icon>
          <input
            v-model="command"
            :placeholder="t('shell.commandPlaceholder')"
            :aria-label="t('shell.commandAria')"
            :disabled="!commandEnabled"
          />
          <kbd v-if="!command">↵</kbd>
        </form>

        <div class="lang-switch" role="group" :aria-label="t('shell.language')">
          <button
            type="button"
            :class="{ 'is-on': locale === 'zh' }"
            @click="setLocale('zh')"
          >中文</button>
          <button
            type="button"
            :class="{ 'is-on': locale === 'en' }"
            @click="setLocale('en')"
          >EN</button>
        </div>
      </header>

      <main class="main-content">
        <router-view />
      </main>
    </div>
  </div>
  </el-config-provider>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import enLocale from 'element-plus/es/locale/lang/en'
import { locale, setLocale, t } from './i18n/index.js'

// What Element Plus says for itself (table "No Data", pagination, date picker).
// Its imperative services -- ElMessageBox, ElMessage -- are created outside the
// provider tree and keep the locale they were installed with, so their buttons
// stay English. That is the existing behaviour, not something this changes.
const epLocale = computed(() => (locale.value === 'zh' ? zhCn : enLocale))

const route = useRoute()
const router = useRouter()
const command = ref('')

const currentRoute = computed(() => {
  const path = route.path
  // Match the parent route for nested paths.
  if (path.startsWith('/customers/')) return '/customers'
  return path
})

const pageLabel = computed(() => {
  const key = route.meta?.titleKey
  return key ? t(key) : ''
})

/** The console runs goals; the command bar is pointless on that page itself. */
const commandEnabled = computed(() => route.path !== '/console')

function submitCommand() {
  const goal = command.value.trim()
  if (!goal) return
  command.value = ''
  router.push({ path: '/console', query: { goal } })
}
</script>

<style scoped>
.layout {
  display: flex;
  min-height: 100vh;
}

.shell-main {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

/* A slim bar, not a second header: the page keeps its own H1, this only
 * carries the section crumb and the two global controls. */
.topbar {
  position: sticky;
  top: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  gap: var(--space-4);
  height: 52px;
  padding: 0 var(--space-8);
  background: color-mix(in srgb, var(--bg-card) 88%, transparent);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--border-subtle);
}

.topbar-crumb {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-caption);
  color: var(--text-3);
  white-space: nowrap;
}
.crumb-agent {
  letter-spacing: 0.08em;
  font-weight: 600;
  color: var(--text-2);
}
.crumb-sep {
  color: var(--border);
}
.crumb-page {
  color: var(--text-1);
}

.topbar-command {
  flex: 1 1 auto;
  max-width: 560px;
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: 34px;
  padding: 0 var(--space-3);
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-subtle);
  transition: border-color 120ms ease, background 120ms ease;
}
.topbar-command:focus-within {
  border-color: var(--accent);
  background: var(--bg-card);
}
.command-icon {
  color: var(--text-3);
  font-size: 14px;
}
.topbar-command input {
  flex: 1 1 auto;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  font-size: var(--fs-sm);
  color: var(--text-1);
}
.topbar-command input::placeholder {
  color: var(--text-disabled);
}
.topbar-command kbd {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-disabled);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  line-height: 1.5;
}

.lang-switch {
  display: inline-flex;
  padding: 2px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-subtle);
}
.lang-switch button {
  border: none;
  background: transparent;
  font-size: var(--fs-caption);
  color: var(--text-3);
  padding: 2px 10px;
  border-radius: 999px;
  cursor: pointer;
}
.lang-switch button.is-on {
  background: var(--bg-card);
  color: var(--text-1);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(16, 24, 40, 0.08);
}

.sidebar-footer {
  letter-spacing: 0.01em;
}
</style>
