<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useEmployee } from '../composables/useEmployee'
import { EMPLOYEES, ROLE_LABELS } from '../mock/employees'

const { current, switchEmployee } = useEmployee()
const route = useRoute()
const router = useRouter()

const empId = computed({
  get: () => current.empId,
  set: (v) => switchEmployee(v),
})

const roleLabel = computed(() => ROLE_LABELS[current.role] || current.role)
</script>

<template>
  <div class="app-header">
    <div class="brand">
      <span class="logo">马小i</span>
      <span class="sub">企业智能助手</span>
    </div>

    <div class="emp-switch">
      <el-select
        v-model="empId"
        filterable
        size="default"
        style="width: 240px"
        placeholder="切换员工"
      >
        <el-option
          v-for="e in EMPLOYEES"
          :key="e.empId"
          :label="`${e.name}（${e.empId}）`"
          :value="e.empId"
        >
          <div class="opt-row">
            <span>{{ e.name }}</span>
            <span class="opt-meta">{{ e.department }} · {{ e.position }}</span>
          </div>
        </el-option>
      </el-select>
      <el-tag size="small" type="info" effect="plain">{{ roleLabel }}</el-tag>
      <span class="dept">{{ current.department }}</span>
    </div>

    <el-menu
      :default-active="route.name"
      mode="horizontal"
      :ellipsis="false"
      class="nav"
      router
    >
      <el-menu-item index="chat" :route="{ name: 'chat' }">智能对话</el-menu-item>
      <el-menu-item index="upload" :route="{ name: 'upload' }">文档管理</el-menu-item>
    </el-menu>
  </div>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 0 20px;
  height: 56px;
  background: #1f3a93;
  color: #fff;
  flex-shrink: 0;
}
.brand {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.logo {
  font-size: 18px;
  font-weight: 600;
}
.sub {
  font-size: 12px;
  opacity: 0.85;
}
.emp-switch {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dept {
  font-size: 12px;
  opacity: 0.85;
}
.opt-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.opt-meta {
  font-size: 12px;
  color: #909399;
}
.nav {
  margin-left: auto;
  background: transparent;
  border-bottom: none;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: rgba(255, 255, 255, 0.85);
  --el-menu-active-color: #fff;
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.12);
}
.nav :deep(.el-menu-item) {
  border-bottom: none !important;
}
.nav :deep(.el-menu-item.is-active) {
  border-bottom: 2px solid #fff !important;
}
</style>
