<template>
  <div class="layout">
    <!-- Sidebar -->
    <aside class="sidebar">
      <div class="sidebar-header">
        <h1>TradeAgent</h1>
        <p>AI外贸智能获客助手</p>
      </div>
      <el-menu
        :default-active="currentRoute"
        :router="true"
        background-color="transparent"
        text-color="rgba(255,255,255,0.65)"
        active-text-color="#fff"
      >
        <el-menu-item index="/dashboard">
          <el-icon><DataBoard /></el-icon>
          <span>数据看板</span>
        </el-menu-item>
        <el-menu-item index="/customers">
          <el-icon><User /></el-icon>
          <span>客户管理</span>
        </el-menu-item>
        <el-menu-item index="/emails">
          <el-icon><Message /></el-icon>
          <span>邮件营销</span>
        </el-menu-item>
        <el-menu-item index="/templates">
          <el-icon><Document /></el-icon>
          <span>邮件模板</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><Calendar /></el-icon>
          <span>待办事项</span>
        </el-menu-item>
        <el-menu-item index="/agent">
          <el-icon><MagicStick /></el-icon>
          <span>AI Agent</span>
        </el-menu-item>
      </el-menu>
      <div class="sidebar-footer">
        <p>TradeAgent v1.0</p>
        <p>Copyright (c) 2026 Zeng. All rights reserved.</p>
      </div>
    </aside>

    <!-- Main Content -->
    <main class="main-content">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const currentRoute = computed(() => {
  const path = route.path
  // Match parent route for nested routes
  if (path.startsWith('/customers/')) return '/customers'
  return path
})
</script>

<style scoped>
.sidebar-footer {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  padding: 16px 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
  text-align: center;
}

.sidebar-footer p {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.3);
  line-height: 1.6;
}
</style>