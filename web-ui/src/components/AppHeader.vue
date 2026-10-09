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

// 能进"变更审批"页的角色: 与后端 DATAOPS_APPROVER_ROLES 的**默认值**同口径。
// 这里只是入口可见性, 不是权限判定 —— 服务端每个动作都重新查角色与"审批人≠发起人"，
// 所以配置改了不重建前端也不会造成越权, 最多是菜单多/少一项。
const APPROVER_ROLES = ['hr', 'finance', 'admin']
const showDataOps = computed(() => APPROVER_ROLES.includes(current.role))
</script>

<template>
  <div class="app-header">
    <div class="brand">
      <span class="logo">马小i</span>
      <span class="sub">企业智能助手</span>
    </div>

    <div class="emp-switch">
      <el-select v-model="empId" filterable size="default" style="width: 240px" placeholder="切换员工">
        <el-option v-for="e in EMPLOYEES" :key="e.empId" :label="`${e.name}（${e.empId}）`" :value="e.empId">
          <div class="opt-row">
            <span>{{ e.name }}</span>
            <span class="opt-meta">{{ e.department }} · {{ e.position }}</span>
          </div>
        </el-option>
      </el-select>
      <el-tag size="small" type="info" effect="plain">{{ roleLabel }}</el-tag>
      <span class="dept">{{ current.department }}</span>
    </div>

    <el-menu :default-active="route.name" mode="horizontal" :ellipsis="false" class="nav" router>
      <el-menu-item index="chat" :route="{ name: 'chat' }">智能对话</el-menu-item>
      <el-menu-item index="upload" :route="{ name: 'upload' }">文档管理</el-menu-item>
      <el-menu-item index="graph" :route="{ name: 'graph' }">知识图谱</el-menu-item>
      <el-menu-item index="memory" :route="{ name: 'memory' }">我的记忆</el-menu-item>
      <el-menu-item v-if="showDataOps" index="dataops" :route="{ name: 'dataops' }">变更审批</el-menu-item>
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
  overflow: hidden;
  /* 窄窗口下裁掉导航而非撑破两侧 */
}

.brand {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-shrink: 0;
  white-space: nowrap;
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
  flex-shrink: 0;
}

/* 窄屏下收窄员工选择器, 优先保证品牌与导航可见 */
@media (max-width: 1100px) {
  .emp-switch :deep(.el-select) {
    width: 160px !important;
  }

  /* 窄屏下先舍掉最后的"我的记忆", 靠 URL 直达 */
  .nav :deep(.el-menu-item[index='memory']) {
    display: none;
  }
}

@media (max-width: 900px) {

  .sub,
  .dept {
    display: none;
  }

  .app-header {
    gap: 12px;
  }
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
