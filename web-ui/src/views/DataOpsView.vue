<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { useEmployee } from '../composables/useEmployee'

const { current } = useEmployee()

// 状态 -> 中文名与标签色: 与后端 app/db/dataops.py 的状态字面量一一对应。
const STATUS_LABELS = {
    PENDING_CONFIRM: { label: '待确认', type: 'warning' },
    PENDING_APPROVAL: { label: '待审批', type: 'primary' },
    NEED_REVIEW: { label: '需复核', type: 'info' },
    DENIED: { label: '已拒绝', type: 'danger' },
    EXECUTED: { label: '已执行', type: 'success' },
    ROLLED_BACK: { label: '已回滚', type: 'success' },
    EXPIRED: { label: '已过期', type: 'info' },
    FAILED: { label: '执行失败', type: 'danger' },
}

const ACTION_LABELS = { update: '更新', delete: '软删除', insert: '新增' }

const loading = ref(false)
const activeTab = ref('ops')
const ops = ref([])
const records = ref([])
const detail = ref(null)
const drawerOpen = ref(false)
const images = ref([])

const pendingCount = computed(
    () => ops.value.filter((o) => o.status === 'PENDING_APPROVAL' || o.status === 'PENDING_CONFIRM').length
)

function statusOf(value) {
    return STATUS_LABELS[value] || { label: value, type: 'info' }
}

async function loadOps() {
    loading.value = true
    try {
        const resp = await fetch(
            `/api/dataops?user_id=${encodeURIComponent(current.empId)}&role=${encodeURIComponent(current.role)}&limit=50`
        )
        if (resp.ok) ops.value = (await resp.json()).dataops || []
        else ElMessage.error(`待办加载失败(${resp.status})`)
    } catch {
        ElMessage.error('待办加载失败: 网关不可用')
    } finally {
        loading.value = false
    }
}

async function loadAudit() {
    try {
        const resp = await fetch(
            `/api/dataops/audit?operator=${encodeURIComponent(current.empId)}&role=${encodeURIComponent(current.role)}&limit=50`
        )
        if (resp.ok) records.value = (await resp.json()).records || []
    } catch {
        /* 审计回查是辅助视图, 失败时保留上一次结果 */
    }
}

async function openRow(row) {
    detail.value = row
    images.value = []
    drawerOpen.value = true
    try {
        const resp = await fetch(`/api/dataops/${row.op_id}/images`)
        if (resp.ok) images.value = (await resp.json()).images || []
    } catch {
        /* 镜像只在已执行后才有, 待审批时取不到是正常的 */
    }
}

async function act(row, action, withNote) {
    let note = ''
    if (withNote) {
        try {
            const input = await ElMessageBox.prompt(
                action === 'approve'
                    ? '批准后服务端会按变更前镜像留档并执行, 该操作可回滚。请填写审批意见(会进审计)。'
                    : '请填写驳回理由(会进审计, 不会改动任何数据)。',
                action === 'approve' ? '批准执行' : '驳回变更',
                { confirmButtonText: '提交', cancelButtonText: '取消', inputPlaceholder: '审批意见' }
            )
            note = input.value || ''
        } catch {
            return
        }
    }
    try {
        const resp = await fetch(`/api/dataops/${row.op_id}/${action}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ operator: current.empId, role: current.role, note }),
        })
        const body = await resp.json().catch(() => ({}))
        if (!resp.ok) {
            ElMessage.error(body.detail || `操作失败(${resp.status})`)
            return
        }
        if (action === 'approve') ElMessage.success(`已执行, 影响 ${body.rows_affected} 行`)
        else if (action === 'rollback') ElMessage.success(`已按镜像回写 ${body.rows_restored} 行`)
        else ElMessage.success('已驳回, 未改动任何数据')
        drawerOpen.value = false
        await loadOps()
        await loadAudit()
    } catch {
        ElMessage.error('操作失败: 网关不可用')
    }
}

onMounted(() => {
    loadOps()
    loadAudit()
})
</script>

<template>
    <div class="page">
        <div class="head">
            <div class="title">
                数据变更审批台
                <el-tag size="small" type="info" effect="plain">当前：{{ current.name }}（{{ current.department }}）</el-tag>
            </div>
            <div class="tools">
                <el-tag size="small" :type="pendingCount ? 'warning' : 'success'">
                    待办 {{ pendingCount }} 项
                </el-tag>
                <el-button :icon="Refresh" size="small" @click="loadOps(); loadAudit()">刷新</el-button>
            </div>
        </div>

        <el-alert type="info" :closable="false" show-icon
            title="这里的每个计划都只写了「改哪张表的哪些行」，SQL 由服务端生成；租户/部门谓词由服务端注入，批准前后都不可能被改写。" class="note" />

        <el-tabs v-model="activeTab" class="tabs">
            <el-tab-pane label="变更计划" name="ops">
                <el-table v-loading="loading" :data="ops" size="small" empty-text="暂无数据变更计划" @row-click="openRow">
                    <el-table-column label="状态" width="96">
                        <template #default="{ row }">
                            <el-tag size="small" :type="statusOf(row.status).type">{{ statusOf(row.status).label
                                }}</el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="动作" width="86">
                        <template #default="{ row }">{{ ACTION_LABELS[row.action] || row.action }}</template>
                    </el-table-column>
                    <el-table-column prop="entity" label="实体" width="150" />
                    <el-table-column label="影响行数" width="92">
                        <template #default="{ row }">
                            {{ row.status === 'EXECUTED' ? row.rows_affected : row.est_rows }}
                        </template>
                    </el-table-column>
                    <el-table-column prop="actor_user_id" label="发起人" width="96" />
                    <el-table-column prop="dept_scope" label="部门范围" width="120" show-overflow-tooltip />
                    <el-table-column prop="preview" label="变更说明（给人看的回显）" min-width="300" show-overflow-tooltip />
                    <el-table-column prop="created_at" label="提交时间" width="160" />
                    <el-table-column label="操作" width="220" fixed="right">
                        <template #default="{ row }">
                            <el-button v-if="row.can_approve" size="small" type="primary"
                                @click.stop="act(row, 'approve', true)">批准</el-button>
                            <el-button v-if="row.can_approve" size="small"
                                @click.stop="act(row, 'reject', true)">驳回</el-button>
                            <el-button v-if="row.status === 'EXECUTED' && !row.can_approve" size="small"
                                @click.stop="act(row, 'rollback', false)">回滚</el-button>
                            <el-button size="small" text @click.stop="openRow(row)">详情</el-button>
                        </template>
                    </el-table-column>
                </el-table>
            </el-tab-pane>

            <el-tab-pane label="审计回查" name="audit">
                <el-table :data="records" size="small" empty-text="暂无审计记录">
                    <el-table-column prop="ts" label="时间" width="170" />
                    <el-table-column prop="user_id" label="用户" width="96" />
                    <el-table-column label="决策" width="96">
                        <template #default="{ row }">
                            <el-tag size="small" :type="row.policy_decision === 'deny' ? 'danger' : 'success'">
                                {{ row.policy_decision }}
                            </el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column prop="rows_affected" label="行数" width="72" />
                    <el-table-column prop="decision_reason" label="理由" min-width="220" show-overflow-tooltip />
                    <el-table-column prop="final_sql" label="最终 SQL" min-width="260" show-overflow-tooltip />
                </el-table>
            </el-tab-pane>
        </el-tabs>

        <el-drawer v-model="drawerOpen" title="写计划详情" size="46%">
            <div v-if="detail" class="detail">
                <h4>给人看的回显</h4>
                <p class="preview">{{ detail.preview }}</p>

                <h4>基本信息</h4>
                <el-descriptions :column="1" border size="small">
                    <el-descriptions-item label="计划号">{{ detail.op_id }}</el-descriptions-item>
                    <el-descriptions-item label="状态">{{ statusOf(detail.status).label }}</el-descriptions-item>
                    <el-descriptions-item label="动作 / 实体">
                        {{ ACTION_LABELS[detail.action] || detail.action }} / {{ detail.entity }}
                    </el-descriptions-item>
                    <el-descriptions-item label="预演影响行数">{{ detail.est_rows }}</el-descriptions-item>
                    <el-descriptions-item label="实际影响行数">{{ detail.rows_affected }}</el-descriptions-item>
                    <el-descriptions-item label="发起人">{{ detail.actor_user_id }}（{{ detail.actor_role
                        }}）</el-descriptions-item>
                    <el-descriptions-item label="部门范围">{{ detail.dept_scope }}</el-descriptions-item>
                    <el-descriptions-item label="用户原句">{{ detail.nl_question || '-' }}</el-descriptions-item>
                    <el-descriptions-item label="变更理由">{{ detail.reason || '-' }}</el-descriptions-item>
                    <el-descriptions-item label="决策说明">{{ detail.decision_reason || '-' }}</el-descriptions-item>
                    <el-descriptions-item label="审批人">{{ detail.approver_id || '-' }}</el-descriptions-item>
                    <el-descriptions-item label="过期时间">{{ detail.expires_at || '-' }}</el-descriptions-item>
                </el-descriptions>

                <h4>服务端生成的最终 SQL</h4>
                <pre class="sql">{{ detail.final_sql }}</pre>
                <p class="hint">
                    注意其中的 <code>tenant_id = :scope_tenant</code> 与
                    <code>dept_id = ANY(string_to_array(:scope_depts, ','))</code>：值由服务端绑定，
                    模型与客户端都无法改写它们。
                </p>

                <template v-if="images.length">
                    <h4>变更前镜像（前 {{ images.length }} 行）</h4>
                    <el-table :data="images" size="small">
                        <el-table-column label="主键" min-width="150">
                            <template #default="{ row }">
                                <code>{{ JSON.stringify(row.pk) }}</code>
                            </template>
                        </el-table-column>
                        <el-table-column label="改前值" min-width="260">
                            <template #default="{ row }">
                                <code>{{ JSON.stringify(row.before, null, 0) }}</code>
                            </template>
                        </el-table-column>
                    </el-table>
                </template>
            </div>
        </el-drawer>
    </div>
</template>

<style scoped>
.page {
    flex: 1;
    min-height: 0;
    overflow: auto;
    padding: 16px 22px;
}

.head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 10px;
}

.title {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 16px;
    font-weight: 600;
}

.tools {
    display: flex;
    align-items: center;
    gap: 10px;
}

.note {
    margin-bottom: 12px;
}

.detail h4 {
    margin: 16px 0 8px;
    font-size: 13px;
    color: #303133;
}

.preview {
    margin: 0;
    padding: 10px 12px;
    background: #fdf6ec;
    border-left: 3px solid #e6a23c;
    line-height: 1.7;
    font-size: 13px;
}

.sql,
.detail code {
    font-family: ui-monospace, Consolas, Menlo, monospace;
    font-size: 12px;
}

.sql {
    margin: 0;
    padding: 10px 12px;
    background: #f5f7fa;
    border: 1px solid #e4e7ed;
    border-radius: 4px;
    white-space: pre-wrap;
    word-break: break-all;
    line-height: 1.6;
}

.hint {
    margin: 6px 0 0;
    font-size: 12px;
    color: #909399;
    line-height: 1.7;
}
</style>
