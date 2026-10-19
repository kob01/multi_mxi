<script setup>
// 智能问数的反馈面: 点赞 / 点踩 / 改口径(edit_dsl) / 复制 / 要人看(工单)。
//
// 这些按钮是 badcase 归因的人工入口: 后端按留痕里的错误码、候选分数、verdict 与
// **用户改了什么**(edit_dsl 的 diff)做规则归因, 再产出"补词草案/评测集草案"。
// 所以每条都必须带 query_id(它在 payload 里, 由 AskData_Agent 写完留痕后回传),
// 没有它这条反馈就无处归因 —— 此时按钮要显式不可用而不是"点了没反应"。
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
    queryId: { type: String, default: '' },
    payload: { type: Object, default: () => ({}) },
    userId: { type: String, default: '' },
    role: { type: String, default: 'employee' },
    traceId: { type: String, default: '' },
    sessionId: { type: String, default: '' },
    disabled: { type: Boolean, default: false },
})

const sent = ref('')
const busy = ref(false)
const note = ref('')
const panelOpen = ref(false)
const ticket = ref(null)

const metrics = computed(() => (props.payload.metrics || []).slice(0, 4))
const chosenMetric = ref('')
// 时间区间与是否下钻: edit_dsl 的归因判据就是"改了哪一层的哪个字段", 只给一个
// "口径不对"的文字反馈是归不到位的。
const TIME_TOKENS = [
    { token: '', label: '不改时间' },
    { token: 'this_month', label: '本月' },
    { token: 'last_month', label: '上月' },
    { token: 'this_quarter', label: '本季度' },
    { token: 'this_year', label: '今年' },
    { token: 'all_time', label: '全部历史' },
]
const chosenTime = ref('')
const noDrill = ref(false)

const currentDsl = computed(() => props.payload.dsl || {})

// SQL 排障视图: 对话载荷里只有 query_id(不把每轮 SQL 塞进 SSE/历史), 点开时才按需回查。
const sqlOpen = ref(false)
const sqlLoading = ref(false)
const sqlDetail = ref({})
const sqlText = computed(() => sqlDetail.value.final_sql || '')
const dslText = computed(() => {
    const dsl = sqlDetail.value.dsl_json || props.payload.dsl
    return dsl && Object.keys(dsl).length ? JSON.stringify(dsl, null, 2) : ''
})
const sqlEmptyReason = computed(() => {
    const row = sqlDetail.value
    if (row.cache_hit) return '本轮命中了结果缓存, 没有重新编译 SQL(结果行直接复用)。'
    if (row.verdict === 'clarify') return '本轮被澄清门拦下, 还没进入编译, 因此没有 SQL。'
    if (row.verdict === 'denied') return '本轮被权限或校验拒绝, 没有可执行的 SQL。'
    if (row.verdict === 'fail') return '本轮没有执行成功(见错误码), 可能编译失败或引擎报错。'
    return '这一轮没有记录到 SQL。'
})

async function openSql() {
    if (!props.queryId) return
    sqlOpen.value = true
    sqlLoading.value = true
    // 先用载荷里已有的 route/verdict/cache_hit 顶上, 等详情回来再替换。
    sqlDetail.value = { ...props.payload, query_id: props.queryId }
    try {
        const params = new URLSearchParams({ role: props.role || 'employee', operator: props.userId || '' })
        const resp = await fetch(`/api/askdata/queries/${encodeURIComponent(props.queryId)}?${params.toString()}`)
        const data = await resp.json().catch(() => ({}))
        if (!resp.ok) throw new Error(data.detail || `读取失败(${resp.status})`)
        sqlDetail.value = data
    } catch (e) {
        ElMessage.error('读取 SQL 失败: ' + e.message)
        sqlOpen.value = false
    } finally {
        sqlLoading.value = false
    }
}

async function copySql() {
    if (!sqlText.value) return
    try {
        await navigator.clipboard.writeText(sqlText.value)
        ElMessage.success('已复制 SQL')
    } catch {
        ElMessage.warning('浏览器不允许直接写剪贴板, 请手动选择复制')
    }
}

// 把"用户的三处改动"合成本轮 DSL 的修正版: 没动的字段一律沿用原值, 这样后端
// dsl_diff 只会看到真正被改的那一项, 不会把我们的默认值当成"用户改了它"。
const correctedDsl = computed(() => {
    const base = JSON.parse(JSON.stringify(currentDsl.value || {}))
    if (chosenMetric.value) base.metrics = [{ id: chosenMetric.value }]
    if (chosenTime.value) base.time = { ...(base.time || {}), range: chosenTime.value }
    if (noDrill.value) delete base.dims
    return base
})

async function post(path, body) {
    const resp = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error(data.detail || `${path} 调用失败`)
    return data
}

async function submit(kind) {
    if (!props.queryId) {
        ElMessage.warning('这一轮没有留痕号, 反馈无法归因(可能问数服务未开启留痕)')
        return
    }
    busy.value = true
    try {
        const body = {
            query_id: props.queryId,
            kind,
            user_id: props.userId,
            note: note.value || '',
        }
        if (kind === 'rewritten' && (chosenMetric.value || chosenTime.value || noDrill.value)) {
            body.corrected_dsl = correctedDsl.value
        }
        const data = await post('/api/askdata/feedback', body)
        sent.value = kind
        panelOpen.value = false
        ElMessage.success(
            data.category ? `已记录（归因：${data.category}）` : '已记录，感谢反馈'
        )
    } catch (e) {
        ElMessage.error('反馈提交失败: ' + e.message)
    } finally {
        busy.value = false
    }
}

// 结构化事件: copy/drill/edit_dsl 这类"行为信号"与点赞点踩分开走 /events,
// 因为它们不代表满意与否(复制了也可能是怀疑), 混进 feedback 会污染点踩率。
async function event(type, payload = {}) {
    try {
        await post('/api/askdata/events', {
            query_id: props.queryId || '',
            trace_id: props.traceId || '',
            session_id: props.sessionId || '',
            user_id: props.userId,
            event_type: type,
            payload,
        })
    } catch (e) {
        // 事件写不进去不该打断用户操作: 它只是多一份证据, 不是功能本身。
        ElMessage.warning('这条行为信号没能记上: ' + e.message)
    }
}

async function copyNumbers() {
    const columns = props.payload.columns || []
    const rows = (props.payload.rows || []).slice(0, 50)
    const labels = props.payload.column_labels || {}
    const head = '| ' + columns.map((c) => labels[c] || c).join(' | ') + ' |'
    const lines = rows.map((r) => '| ' + columns.map((c) => r[c] ?? '-').join(' | ') + ' |')
    const text = [head, ...lines].join('\n')
    try {
        await navigator.clipboard.writeText(text)
    } catch {
        ElMessage.warning('浏览器不允许直接写剪贴板, 请手动选择表格复制')
    }
    if (props.queryId) await event('copy', { rows: rows.length })
}

async function askHuman() {
    if (!props.queryId) {
        ElMessage.warning('这一轮没有留痕号, 工单挂不上那一次问数')
        return
    }
    busy.value = true
    try {
        const data = await post('/api/askdata/tickets?role=employee', {
            question: props.payload.question || '',
            reason: note.value.trim() ? 'bad_answer' : 'user_request',
            query_id: props.queryId,
            trace_id: props.traceId || '',
            dsl: currentDsl.value,
            semver: props.payload.semver || '',
            requested_by: props.userId,
        })
        ticket.value = data
        panelOpen.value = false
        ElMessage.success(`已开人工工单 ${data.ticket_id}，处理结果会回到治理台`)
    } catch (e) {
        ElMessage.error('开单失败: ' + e.message)
    } finally {
        busy.value = false
    }
}

function openRewrite() {
    panelOpen.value = true
    chosenMetric.value = ''
    chosenTime.value = ''
    noDrill.value = false
    note.value = ''
    ticket.value = null
}
</script>

<template>
    <div v-if="queryId" class="feedback">
        <template v-if="!sent">
            <el-button size="small" text :disabled="disabled || busy" @click="submit('up')">
                👍 准
            </el-button>
            <el-button size="small" text :disabled="disabled || busy" @click="submit('down')">
                👎 不准
            </el-button>
            <el-button size="small" text :disabled="disabled || busy" @click="openRewrite">
                ✏️ 我要改口径
            </el-button>
            <el-button size="small" text :disabled="disabled" @click="copyNumbers">
                📋 复制数字
            </el-button>
            <el-button size="small" text :disabled="disabled" @click="openSql">
                🧾 查看 SQL
            </el-button>
        </template>
        <span v-else class="done">已收到反馈，会进入 badcase 归因(生成的口径草案需人工核对后生效)</span>

        <el-dialog v-model="panelOpen" title="这个数哪里不对？" width="460px">
            <div class="row">
                <span class="label">改选指标口径</span>
                <el-select v-model="chosenMetric" size="small" placeholder="从刚才的候选里选" clearable>
                    <el-option v-for="m in metrics" :key="m.id" :value="m.id" :label="`${m.label}（${m.id}）`" />
                </el-select>
            </div>
            <div class="row">
                <span class="label">统计区间应该是</span>
                <el-select v-model="chosenTime" size="small">
                    <el-option v-for="t in TIME_TOKENS" :key="t.token" :value="t.token" :label="t.label" />
                </el-select>
            </div>
            <div class="row">
                <el-checkbox v-model="noDrill" size="small">不该按维度拆开（要的是一个数）</el-checkbox>
            </div>
            <div class="row">
                <span class="label">补充说明</span>
                <el-input v-model="note" size="small" type="textarea" :rows="2" placeholder="例如：应该只算已打款的，不含待审" />
            </div>
            <div v-if="currentDsl.metrics && !ticket" class="diff">
                提交后系统会把你改的字段与本轮 DSL 做 diff 来定归因类别；只改一项就只归一项。
            </div>
            <template #footer>
                <el-button size="small" @click="panelOpen = false">取消</el-button>
                <el-button size="small" :loading="busy" @click="askHuman">
                    开个工单请人看
                </el-button>
                <el-button size="small" type="primary" :loading="busy" @click="submit('rewritten')">
                    提交修正
                </el-button>
            </template>
        </el-dialog>

        <el-dialog v-model="sqlOpen" title="这一轮实际执行的 SQL" width="720px">
            <div v-loading="sqlLoading">
                <div class="sql-meta">
                    <span>路由 {{ sqlDetail.route || '—' }}</span>
                    <span>结论 {{ sqlDetail.verdict || '—' }}{{ sqlDetail.cache_hit ? '（缓存命中）' : '' }}</span>
                    <span>行数 {{ sqlDetail.rowcount ?? 0 }}</span>
                    <span>口径版本 {{ sqlDetail.semver || '—' }}</span>
                </div>
                <pre v-if="sqlText" class="sql-block">{{ sqlText }}</pre>
                <el-alert v-else type="info" :closable="false" :title="sqlEmptyReason" />
                <template v-if="dslText">
                    <div class="label sql-title">本轮 DSL（模型产物，SQL 由编译器按语义层生成）</div>
                    <pre class="sql-block">{{ dslText }}</pre>
                </template>
            </div>
            <template #footer>
                <el-button size="small" @click="sqlOpen = false">关闭</el-button>
                <el-button size="small" type="primary" :disabled="!sqlText" @click="copySql">复制 SQL</el-button>
            </template>
        </el-dialog>
    </div>
</template>

<style scoped>
.feedback {
    margin-top: 6px;
    display: flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
}

.done {
    font-size: 12px;
    color: #67c23a;
}

.row {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-bottom: 12px;
}

.label {
    font-size: 12px;
    color: #6b7280;
}

.diff {
    font-size: 12px;
    color: #909399;
}

.sql-meta {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 8px;
    font-size: 12px;
    color: #6b7280;
}

.sql-title {
    display: block;
    margin-top: 10px;
}

.sql-block {
    margin: 0;
    padding: 10px 12px;
    background: #f6f8fa;
    border: 1px solid #ebeef5;
    border-radius: 6px;
    font-family: Consolas, 'Courier New', monospace;
    font-size: 12px;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 320px;
    overflow: auto;
}
</style>
