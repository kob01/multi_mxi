<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { useEmployee } from '../composables/useEmployee'

const { current } = useEmployee()

// 窗口天数: 留痕按天滚, 看 7 天是常态, 复盘时拉到 30/90。
const days = ref(7)
const DAY_OPTIONS = [7, 14, 30, 90]

const loading = ref(false)
const activeTab = ref('metrics')

const stats = ref({})
const alerts = ref([])
const semantic = ref({})
const tickets = ref([])
const ticketStatus = ref('open')
const samples = ref([])
const sampleStatus = ref('draft')
const sampleStats = ref({})
const badcases = ref([])
const golden = ref([])
const goldenFilter = ref('all')

// 问数记录: SQL 不随对话载荷/SSE 下发, 而是在这里按需回查一轮的 final_sql 与 DSL。
const queries = ref([])
const queryVerdict = ref('')
const queryUser = ref('')
const queryOpen = ref(false)
const queryLoading = ref(false)
const queryDetail = ref({})

// 指标卡: key 必须与 app/askdata/store.py::stats 返回的字段名一致。
// 刻意把"分母是什么"写进 tip: 这些比率的分母互不相同(全员轮次 / 澄清轮次 / 有反馈轮次),
// 不看说明就会把"误澄清率 100%"读成"一半的问数都错了" —— 那是两个不同的病。
const RATE_CARDS = [
  { key: 'first_pass_rate', label: '一次通过率', tip: '分母=进入状态机的轮次(含被澄清与被拒)' },
  { key: 'clarify_rate', label: '澄清触发率', tip: '问了几个: 分母=全部轮次' },
  { key: 'mis_clarify_rate', label: '误澄清率', tip: '这些反问里有多少本可以不问(分母=澄清轮次)' },
  { key: 'degrade_rate', label: '降级率', tip: '走到沙箱或自修复: 前两级覆盖不住了' },
  { key: 'fallback_rate', label: 'FALLBACK 率', tip: '彻底没给出数的比例' },
  { key: 'fail_rate', label: '执行/权限错误率', tip: '数算不出来的比例' },
  { key: 'thumbs_down_rate', label: '点踩率', tip: '分母=给过反馈的轮次, 不是全部轮次' },
  { key: 'feedback_rate', label: '反馈率', tip: '愿意给反馈的轮次占比(太低说明看板不可信)' },
  { key: 'cache_hit_rate', label: '结果缓存命中', tip: '同一意图+同一身份作用域复用结果行' },
  { key: 'sem_cache_hit_rate', label: '语义缓存命中', tip: '复用已批准的查询意图(不省校验)' },
]

const EXTRA_CARDS = [
  { key: 'p95_latency_ms', label: 'P95 时延', suffix: 'ms' },
  { key: 'avg_latency_ms', label: '平均时延', suffix: 'ms' },
  { key: 'token_total', label: '累计 tokens', suffix: '' },
]

const RULE_LABELS = {
  fail_rate: '执行/权限错误率',
  degrade_rate: '降级率',
  clarify_rate: '澄清触发率',
  p95_latency_ms: 'P95 时延(ms)',
  eval_gate: '评测 CI 门禁',
  golden_size: '评测集规模(条)',
}

const TICKET_LABELS = {
  no_metric: '语义层没这个口径',
  no_data: '查不到数据',
  budget_exceeded: '超预算熔断',
  engine_error: '引擎错误',
  user_request: '用户主动要人',
  bad_answer: '答案被判不好',
}

const CATEGORY_LABELS = {
  intent: '①意图理解',
  link: '②语义链接',
  dsl: '③DSL 生成',
  exec: '④执行/权限',
  render: '⑤呈现',
}

const DISPOSITION_LABELS = {
  add_term: '补别名(草案)',
  add_metric: '补指标(草案)',
  fix_compat: '修兼容规则(草案)',
  golden: '进评测集(草案)',
  ticket: '开人工工单',
  observe: '只观察',
  deny: '权限侧处理',
}

const GATE_STATUS = { green: '通过', red: '阻断', unknown: '未记录' }

const firedAlerts = computed(() => alerts.value.filter((a) => a.fired).length)
const openTickets = computed(() => tickets.value.length)
const draftSamples = computed(() => Number(sampleStats.value.draft || 0))

const evalRun = computed(() => stats.value.latest_eval || {})
const sampleQueue = computed(() => stats.value.samples || {})
const sqlText = computed(() => queryDetail.value.final_sql || '')
// 没有 SQL 时把“为什么没有”说清楚: 缓存命中/被澄清拦下/被拒/执行失败都是不同的事。
const sqlEmptyReason = computed(() => {
  const row = queryDetail.value
  if (row.cache_hit) return '本轮命中了结果缓存, 没有重新编译 SQL(结果行直接复用)。'
  if (row.verdict === 'clarify') return '本轮被澄清门拦下, 还没进入编译, 因此没有 SQL。'
  if (row.verdict === 'denied') return '本轮被权限或校验拒绝, 没有可执行的 SQL。'
  if (row.verdict === 'fail') return '本轮没有执行成功(见错误码与备注), 可能编译失败或引擎报错。'
  return '这一轮没有记录到 SQL。'
})
const dslText = computed(() => {
  const dsl = queryDetail.value.dsl_json
  return dsl && Object.keys(dsl).length ? JSON.stringify(dsl, null, 2) : ''
})

function pct(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return '—'
  return `${Number(value).toFixed(1)}%`
}

function fmtValue(item) {
  const raw = stats.value[item.key]
  if (raw === undefined || raw === null) return '—'
  if (item.suffix === 'ms') return `${Number(raw).toLocaleString()} ms`
  if (item.suffix === '') return Number(raw).toLocaleString()
  return pct(raw)
}

function isRisk(key) {
  // 只有"越低越好"的指标才把高风险标红; 通过率类反向。
  if (key === 'first_pass_rate') return Number(stats.value[key] ?? 100) < 80
  return ['fail_rate', 'degrade_rate', 'fallback_rate', 'thumbs_down_rate', 'mis_clarify_rate'].includes(key)
    && Number(stats.value[key] || 0) > 15
}

function query(url) {
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}role=${encodeURIComponent(current.role)}&operator=${encodeURIComponent(current.empId)}`
}

async function loadAll() {
  loading.value = true
  try {
    const [s, a, sem] = await Promise.all([
      fetch(query(`/api/askdata/stats?days=${days.value}`)),
      fetch(query(`/api/askdata/alerts?days=${days.value}`)),
      fetch(query('/api/askdata/semantic')),
    ])
    if (s.ok) stats.value = await s.json()
    if (a.ok) alerts.value = (await a.json()).alerts || []
    if (sem.ok) semantic.value = await sem.json()
    await Promise.all([loadTickets(), loadSamples(), loadBadcases(), loadGolden(), loadQueries()])
  } catch {
    ElMessage.error('治理数据加载失败: 网关不可用')
  } finally {
    loading.value = false
  }
}

async function loadTickets() {
  try {
    const resp = await fetch(query(`/api/askdata/tickets?status=${ticketStatus.value}&limit=50`))
    tickets.value = resp.ok ? (await resp.json()).tickets || [] : []
  } catch {
    tickets.value = []
  }
}

async function loadSamples() {
  try {
    const resp = await fetch(query(`/api/askdata/samples?status=${sampleStatus.value}&limit=100`))
    if (!resp.ok) {
      samples.value = []
      return
    }
    const body = await resp.json()
    samples.value = body.samples || []
    sampleStats.value = body.stats || {}
  } catch {
    samples.value = []
  }
}

async function loadBadcases() {
  try {
    const resp = await fetch(query(`/api/askdata/badcases?days=${days.value}&limit=50`))
    badcases.value = resp.ok ? (await resp.json()).badcases || [] : []
  } catch {
    badcases.value = []
  }
}

async function loadQueries() {
  try {
    const params = new URLSearchParams({ days: days.value, limit: '100' })
    if (queryVerdict.value) params.set('verdict', queryVerdict.value)
    if (queryUser.value.trim()) params.set('user_id', queryUser.value.trim())
    const resp = await fetch(query(`/api/askdata/queries?${params.toString()}`))
    queries.value = resp.ok ? (await resp.json()).queries || [] : []
  } catch {
    queries.value = []
  }
}

async function openQueryDetail(row) {
  queryOpen.value = true
  queryLoading.value = true
  queryDetail.value = { ...row }
  try {
    const resp = await fetch(query(`/api/askdata/queries/${encodeURIComponent(row.query_id)}`))
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error(body.detail || `读取失败(${resp.status})`)
    queryDetail.value = body
  } catch (e) {
    ElMessage.error('读取 SQL 失败: ' + e.message)
    queryOpen.value = false
  } finally {
    queryLoading.value = false
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

async function loadGolden() {
  try {
    const resp = await fetch(query(`/api/askdata/golden?limit=2000&include_drafts=true`))
    const all = resp.ok ? (await resp.json()).cases || [] : []
    // 默认只列"能进缓存评审范围"的启用用例, 免得草案把列表淹掉。
    golden.value = goldenFilter.value === 'cacheable'
      ? all.filter((c) => c.enabled && !c.cacheable).slice(0, 200)
      : all.slice(0, 200)
  } catch {
    golden.value = []
  }
}

async function patchTicket(row, status) {
  let resolution = ''
  if (status === 'resolved' || status === 'rejected') {
    try {
      const input = await ElMessageBox.prompt(
        status === 'resolved'
          ? '写清结论与依据(会进审计, 也是下一个同问法的人能看到的答案来源)。'
          : '写清为什么驳回(例如问题本身超出语义层治理范围)。',
        status === 'resolved' ? '结办' : '驳回工单',
        { confirmButtonText: '提交', cancelButtonText: '取消', inputPlaceholder: '结论/理由' },
      )
      resolution = input.value || ''
    } catch {
      return
    }
  }
  try {
    const resp = await fetch(`/api/askdata/tickets/${row.ticket_id}?role=${encodeURIComponent(current.role)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, resolution, assigned_to: current.empId, question: row.question, reason: row.reason }),
    })
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) {
      ElMessage.error(body.detail || `工单操作失败(${resp.status})`)
      return
    }
    ElMessage.success(status === 'open' ? '已领取' : status === 'resolved' ? '已结办' : '已驳回')
    await loadTickets()
  } catch {
    ElMessage.error('工单操作失败: 网关不可用')
  }
}

async function reviewSample(row, status) {
  try {
    const resp = await fetch(
      query(`/api/askdata/samples/${row.sample_id}?status=${status}&note=${encodeURIComponent('页面复核')}`),
      { method: 'POST' },
    )
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) {
      ElMessage.error(body.detail || `复核失败(${resp.status})`)
      return
    }
    ElMessage.success(status === 'approved' ? '已批准(导出脚本只会取过门禁的那些)' : '已驳回')
    await loadSamples()
  } catch {
    ElMessage.error('复核失败: 网关不可用')
  }
}

async function toggleCache(row) {
  const next = !row.cacheable
  if (next) {
    try {
      await ElMessageBox.confirm(
        '批准语义缓存复用后, 这句问法的答案会被**语义相近的新问题**直接端出来(校验/编译/权限仍照跑)。'
        + '只有你确认过"这条标准答案就是唯一正确口径"时才批准。',
        '批准参与语义缓存',
        { confirmButtonText: '确认批准', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return
    }
  }
  try {
    const resp = await fetch(
      query(`/api/askdata/golden/${row.case_id}/cacheable?cacheable=${next}`),
      { method: 'POST' },
    )
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) {
      ElMessage.error(body.detail || `操作失败(${resp.status})`)
      return
    }
    ElMessage.success(next ? '已允许复用' : '已收回复用资格')
    await loadGolden()
  } catch {
    ElMessage.error('操作失败: 网关不可用')
  }
}

onMounted(loadAll)
</script>

<template>
  <div class="page">
    <div class="head">
      <div class="title">
        问数治理看板
        <el-tag size="small" type="info" effect="plain">当前：{{ current.name }}（{{ current.department }}）</el-tag>
        <el-tag v-if="semantic.semver" size="small" effect="plain">口径版本 {{ semantic.semver }}</el-tag>
        <el-tag v-if="semantic.catalog && semantic.catalog.origin !== 'db'" size="small" type="warning" effect="plain">
          语义层来自内置兜底集
        </el-tag>
      </div>
      <div class="tools">
        <el-select v-model="days" size="small" style="width: 104px" @change="loadAll">
          <el-option v-for="d in DAY_OPTIONS" :key="d" :label="`近 ${d} 天`" :value="d" />
        </el-select>
        <el-tag size="small" :type="firedAlerts ? 'danger' : 'success'">告警 {{ firedAlerts }} 条</el-tag>
        <el-button :icon="Refresh" size="small" :loading="loading" @click="loadAll">刷新</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" show-icon class="note"
      title="这里的每个比率都只是「观察窗」, 不是放行依据: 放行依据是评测 CI 门禁(golden 的 EX)。改口径请改 semantic-defs/*.yaml 再同步, 不要直接改库。" />

    <el-tabs v-model="activeTab" class="tabs">
      <el-tab-pane label="运行指标" name="metrics">
        <div class="cards">
          <div v-for="item in RATE_CARDS" :key="item.key" class="card" :class="{ risk: isRisk(item.key) }">
            <div class="card-label">{{ item.label }}</div>
            <div class="card-value">{{ fmtValue(item) }}</div>
            <div class="card-tip">{{ item.tip }}</div>
          </div>
          <div v-for="item in EXTRA_CARDS" :key="item.key" class="card">
            <div class="card-label">{{ item.label }}</div>
            <div class="card-value">{{ fmtValue(item) }}</div>
            <div class="card-tip">窗口内 {{ stats.total || 0 }} 轮问数</div>
          </div>
        </div>

        <div class="sub-title">告警线（阈值来自配置，与 GET /metrics 同一份判定）</div>
        <el-table :data="alerts" size="small" empty-text="暂无告警规则">
          <el-table-column label="规则" width="180">
            <template #default="{ row }">{{ RULE_LABELS[row.rule] || row.rule }}</template>
          </el-table-column>
          <el-table-column label="当前值" width="120">
            <template #default="{ row }">
              <span :class="{ danger: row.fired }">{{ row.value }}</span>
            </template>
          </el-table-column>
          <el-table-column label="阈值" width="110">
            <template #default="{ row }">{{ row.threshold }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.fired ? 'danger' : 'success'">
                {{ row.fired ? '越线' : '正常' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="样本数" width="100">
            <template #default="{ row }">{{ row.samples }}</template>
          </el-table-column>
          <el-table-column label="说明" prop="note" min-width="260" show-overflow-tooltip />
        </el-table>

        <div class="sub-title">最近一次评测（EX 是主指标，judge 只能标"疑似等价"不计分）</div>
        <el-descriptions :column="4" border size="small">
          <el-descriptions-item label="模式">{{ evalRun.mode || '—' }}</el-descriptions-item>
          <el-descriptions-item label="用例数">{{ evalRun.case_count ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="EX(执行准确率)">
            <span :class="{ danger: Number(evalRun.exec_accuracy || 0) < 90 }">{{ pct(evalRun.exec_accuracy) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="DSL-EM">{{ pct(evalRun.dsl_exact_match) }}</el-descriptions-item>
          <el-descriptions-item label="slot-F1">{{ evalRun.slot_f1 ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="CI 门禁">
            <el-tag size="small" :type="evalRun.gate_status === 'red' ? 'danger' : 'success'">
              {{ GATE_STATUS[evalRun.gate_status] || '未记录' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="评测口径版本">{{ evalRun.semver || '—' }}</el-descriptions-item>
          <el-descriptions-item label="跑于">{{ (evalRun.started_at || '—').toString().slice(0, 19) }}</el-descriptions-item>
          <el-descriptions-item label="评测集">
            {{ stats.golden_enabled || 0 }} 条启用（下限 {{ semantic.golden_min_required ?? '—' }}）
          </el-descriptions-item>
          <el-descriptions-item label="已批准可复用">{{ stats.golden_cacheable ?? '—' }} 条</el-descriptions-item>
          <el-descriptions-item label="误澄清分子/分母">
            {{ stats.clarify_picked ?? 0 }} 次点选里有 {{ stats.mis_clarify_rate ?? 0 }}% 本可以不问
          </el-descriptions-item>
          <el-descriptions-item label="样本队列">
            草案 {{ sampleQueue.draft ?? 0 }} · 已批准 {{ sampleQueue.approved ?? 0 }} · 被门禁拦住 {{ sampleQueue.gated_out ?? 0 }}
          </el-descriptions-item>
        </el-descriptions>
      </el-tab-pane>

      <el-tab-pane label="问数记录" name="queries">
        <el-alert type="info" :closable="false" show-icon class="note"
          title="SQL 是排障视图：对话与 SSE 只带 query_id, 点「查看 SQL」才按需回查这一轮真正执行的 final_sql 与 DSL。库里只保留前 4000 字符。" />
        <div class="row-tools">
          <el-radio-group v-model="queryVerdict" size="small" @change="loadQueries">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button value="ok">出数</el-radio-button>
            <el-radio-button value="clarify">澄清</el-radio-button>
            <el-radio-button value="fail">失败</el-radio-button>
            <el-radio-button value="denied">被拒</el-radio-button>
            <el-radio-button value="degraded">降级</el-radio-button>
          </el-radio-group>
          <el-input v-model="queryUser" size="small" style="width: 150px" placeholder="按工号过滤" clearable
            @change="loadQueries" />
          <span class="hint">近 {{ days }} 天 · 最多 100 条</span>
        </div>
        <el-table :data="queries" size="small" empty-text="窗口内没有问数记录">
          <el-table-column label="时间" width="160">
            <template #default="{ row }">{{ (row.created_at || '').toString().slice(0, 19).replace('T', ' ') }}</template>
          </el-table-column>
          <el-table-column label="提问者" width="150">
            <template #default="{ row }">
              {{ row.user_id || '—' }}<span class="hint"> / {{ row.role || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="问题" prop="question" min-width="240" show-overflow-tooltip />
          <el-table-column label="路由" width="100">
            <template #default="{ row }">{{ row.route || '—' }}</template>
          </el-table-column>
          <el-table-column label="结论" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.verdict === 'ok' ? 'success' : row.verdict === 'clarify' ? 'warning' : 'danger'">
                {{ row.verdict || '—' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="行数" width="80">
            <template #default="{ row }">{{ row.rowcount ?? 0 }}</template>
          </el-table-column>
          <el-table-column label="时延" width="90">
            <template #default="{ row }">{{ row.latency_ms ? `${row.latency_ms} ms` : '—' }}</template>
          </el-table-column>
          <el-table-column label="缓存" width="70">
            <template #default="{ row }">{{ row.cache_hit ? '命中' : '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="110" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openQueryDetail(row)">查看 SQL</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane name="tickets">
        <template #label>
          人工工单
          <el-badge v-if="activeTab !== 'tickets' && openTickets" :value="openTickets" class="tab-badge" />
        </template>
        <div class="row-tools">
          <el-radio-group v-model="ticketStatus" size="small" @change="loadTickets">
            <el-radio-button value="open">待处理</el-radio-button>
            <el-radio-button value="processing">处理中</el-radio-button>
            <el-radio-button value="resolved">已结办</el-radio-button>
            <el-radio-button value="">全部</el-radio-button>
          </el-radio-group>
        </div>
        <el-table :data="tickets" size="small" empty-text="没有待办工单">
          <el-table-column label="问题" prop="question" min-width="260" show-overflow-tooltip />
          <el-table-column label="来处" width="140">
            <template #default="{ row }">{{ TICKET_LABELS[row.reason] || row.reason }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'resolved' ? 'success' : row.status === 'rejected' ? 'info' : 'warning'">
                {{ row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="口径版本" width="130">
            <template #default="{ row }">{{ row.semver || '—' }}</template>
          </el-table-column>
          <el-table-column label="结论" prop="resolution" min-width="200" show-overflow-tooltip />
          <el-table-column label="操作" width="200" fixed="right">
            <template #default="{ row }">
              <el-button v-if="row.status === 'open'" link type="primary" size="small" @click="patchTicket(row, 'processing')">领取</el-button>
              <el-button v-if="row.status !== 'resolved' && row.status !== 'rejected'" link type="success" size="small" @click="patchTicket(row, 'resolved')">结办</el-button>
              <el-button v-if="row.status !== 'rejected' && row.status !== 'resolved'" link type="danger" size="small" @click="patchTicket(row, 'rejected')">驳回</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane name="samples">
        <template #label>
          训练样本队列
          <el-badge v-if="activeTab !== 'samples' && draftSamples" :value="draftSamples" class="tab-badge" />
        </template>
        <el-alert type="warning" :closable="false" show-icon class="note"
          title="队列里的每一行都要过两道门：chosen 必须真能过校验器并编成 SQL，且用户点过赞或亲自改写过。导出脚本默认只取批准且过双门禁的样本 —— 错的样本比不训更危险。" />
        <div class="row-tools">
          <el-radio-group v-model="sampleStatus" size="small" @change="loadSamples">
            <el-radio-button value="draft">待复核</el-radio-button>
            <el-radio-button value="approved">已批准</el-radio-button>
            <el-radio-button value="exported">已导出</el-radio-button>
            <el-radio-button value="">全部</el-radio-button>
          </el-radio-group>
          <span class="hint">队列合计 {{ sampleQueue.total ?? 0 }} 条</span>
        </div>
        <el-table :data="samples" size="small" empty-text="暂无样本(先积累反馈事件才会产生)">
          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.kind === 'dpo' ? 'warning' : 'primary'">{{ row.kind }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="问法" prop="question" min-width="240" show-overflow-tooltip />
          <el-table-column label="归因" width="120">
            <template #default="{ row }">{{ CATEGORY_LABELS[row.category] || row.category || '—' }}</template>
          </el-table-column>
          <el-table-column label="门禁" width="180">
            <template #default="{ row }">
              <el-tag size="small" :type="row.gate_validated ? 'success' : 'info'">可编译</el-tag>
              <el-tag size="small" :type="row.gate_confirmed ? 'success' : 'info'">人确认</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">{{ row.status }}</template>
          </el-table-column>
          <el-table-column label="口径版本" width="130">
            <template #default="{ row }">{{ row.semver || '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="150" fixed="right">
            <template #default="{ row }">
              <el-button v-if="row.status === 'draft'" link type="success" size="small" @click="reviewSample(row, 'approved')">批准</el-button>
              <el-button v-if="row.status === 'draft'" link type="danger" size="small" @click="reviewSample(row, 'rejected')">驳回</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="待标注 badcase" name="badcases">
        <el-table :data="badcases" size="small" empty-text="近段时间没有被点踩或失败的问数">
          <el-table-column label="问题" prop="question" min-width="240" show-overflow-tooltip />
          <el-table-column label="结论" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.verdict === 'ok' ? 'warning' : 'danger'">{{ row.verdict }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="归因" width="120">
            <template #default="{ row }">{{ CATEGORY_LABELS[row.category] || row.category }}</template>
          </el-table-column>
          <el-table-column label="建议处置" width="150">
            <template #default="{ row }">{{ DISPOSITION_LABELS[row.disposition] || row.disposition }}</template>
          </el-table-column>
          <el-table-column label="路由" width="110">
            <template #default="{ row }">{{ row.route || '—' }}</template>
          </el-table-column>
          <el-table-column label="错误码" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">{{ (row.error_codes || []).join(', ') || '—' }}</template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="评测集与缓存复用" name="golden">
        <el-alert type="info" :closable="false" show-icon class="note"
          title="语义缓存默认只服务这里被逐条批准的问法。合成 golden 只当尺子用：全部放开会让评测题直接拿到标准答案，EX 虚高而 NL→DSL 能力一点没变。" />
        <div class="row-tools">
          <el-radio-group v-model="goldenFilter" size="small" @change="loadGolden">
            <el-radio-button value="all">全部用例</el-radio-button>
            <el-radio-button value="cacheable">待批准复用</el-radio-button>
          </el-radio-group>
          <span class="hint">最多显示 200 条（共 {{ stats.golden_enabled || 0 }} 条启用）</span>
        </div>
        <el-table :data="golden" size="small" empty-text="没有用例">
          <el-table-column label="问法" prop="question" min-width="240" show-overflow-tooltip />
          <el-table-column label="难度" width="80">
            <template #default="{ row }">{{ row.difficulty || '—' }}</template>
          </el-table-column>
          <el-table-column label="意图" width="110">
            <template #default="{ row }">{{ row.intent || '—' }}</template>
          </el-table-column>
          <el-table-column label="启用" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '是' : '草案' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="可复用" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.cacheable ? 'warning' : 'info'">{{ row.cacheable ? '已批准' : '未批准' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="口径版本" width="130">
            <template #default="{ row }">{{ row.semver || '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="150" fixed="right">
            <template #default="{ row }">
              <el-button link :type="row.cacheable ? 'danger' : 'success'" size="small" @click="toggleCache(row)">
                {{ row.cacheable ? '收回资格' : '批准复用' }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="口径与版本" name="semantic">
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="来源">
            <el-tag size="small" :type="semantic.catalog && semantic.catalog.origin === 'db' ? 'success' : 'warning'">
              {{ semantic.catalog && semantic.catalog.origin === 'db' ? '数据库真值' : (semantic.catalog && semantic.catalog.origin) || '—' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="semver">{{ semantic.semver || '—' }}</el-descriptions-item>
          <el-descriptions-item label="已登记指标">{{ semantic.catalog?.metric_count ?? '—' }} 个 / 维度 {{ semantic.catalog?.dimension_count ?? '—' }} 个</el-descriptions-item>
          <el-descriptions-item label="评测集下限">{{ semantic.golden_min_required ?? '—' }} 条</el-descriptions-item>
          <el-descriptions-item label="启用用例">{{ semantic.golden_enabled ?? '—' }} 条</el-descriptions-item>
          <el-descriptions-item label="兼容规则 / 行策略 / 列策略">
            {{ semantic.catalog?.compat_rules ?? 0 }} / {{ semantic.catalog?.row_policies ?? 0 }} / {{ semantic.catalog?.col_policies ?? 0 }}
          </el-descriptions-item>
          <el-descriptions-item label="语义层缺表">
            <el-tag v-if="(semantic.catalog?.missing_tables || []).length" size="small" type="danger">
              {{ (semantic.catalog.missing_tables || []).join(', ') }}
            </el-tag>
            <span v-else>无</span>
          </el-descriptions-item>
          <el-descriptions-item label="口径负责人">见 semantic-defs/*.yaml（改口径必须走同步脚本）</el-descriptions-item>
        </el-descriptions>

        <div class="sub-title">最近口径变更（sem_change_log：谁在什么时候改了哪个对象）</div>
        <el-table :data="semantic.recent_changes || []" size="small" empty-text="暂无口径变更记录">
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ (row.created_at || '').toString().slice(0, 19) }}</template>
          </el-table-column>
          <el-table-column label="对象类型" prop="object_kind" width="180" />
          <el-table-column label="对象" prop="object_id" min-width="220" show-overflow-tooltip />
          <el-table-column label="版本" prop="version" width="80" />
          <el-table-column label="操作人" prop="actor" width="120" />
          <el-table-column label="来处" prop="source" width="110" />
          <el-table-column label="说明" prop="note" min-width="200" show-overflow-tooltip />
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="queryOpen" size="640px" title="问数记录详情">
      <div v-loading="queryLoading">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="问题" :span="2">{{ queryDetail.question || '—' }}</el-descriptions-item>
          <el-descriptions-item label="提问者">
            {{ queryDetail.user_id || '—' }} / {{ queryDetail.role || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="时间">
            {{ (queryDetail.created_at || '—').toString().slice(0, 19).replace('T', ' ') }}
          </el-descriptions-item>
          <el-descriptions-item label="路由">{{ queryDetail.route || '—' }}</el-descriptions-item>
          <el-descriptions-item label="结论">
            {{ queryDetail.verdict || '—' }}{{ queryDetail.cache_hit ? '（缓存命中）' : '' }}
          </el-descriptions-item>
          <el-descriptions-item label="行数 / 时延">
            {{ queryDetail.rowcount ?? 0 }} 行 / {{ queryDetail.latency_ms ? `${queryDetail.latency_ms} ms` : '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="口径版本">{{ queryDetail.semver || '—' }}</el-descriptions-item>
          <el-descriptions-item label="留痕号" :span="2">{{ queryDetail.query_id || '—' }}</el-descriptions-item>
          <el-descriptions-item label="错误码" :span="2">
            {{ (queryDetail.error_codes || []).join(', ') || '—' }}
          </el-descriptions-item>
          <el-descriptions-item v-if="queryDetail.notes" label="备注" :span="2">{{ queryDetail.notes }}</el-descriptions-item>
        </el-descriptions>

        <div class="sub-title">
          最终执行 SQL
          <el-button v-if="sqlText" link type="primary" size="small" @click="copySql">复制</el-button>
        </div>
        <pre v-if="sqlText" class="sql-block">{{ sqlText }}</pre>
        <el-alert v-else type="info" :closable="false" :title="sqlEmptyReason" />

        <template v-if="dslText">
          <div class="sub-title">本轮 DSL（模型产物，SQL 由编译器按语义层生成）</div>
          <pre class="sql-block">{{ dslText }}</pre>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.page {
  padding: 16px 20px 28px;
  height: 100%;
  overflow: auto;
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
  font-weight: 600;
}

.tools {
  display: flex;
  align-items: center;
  gap: 8px;
}

.note {
  margin-bottom: 12px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(184px, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}

.card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fff;
}

.card.risk {
  border-color: #f56c6c;
  background: #fef0f0;
}

.card-label {
  font-size: 12px;
  color: #6b7280;
}

.card-value {
  font-size: 20px;
  font-weight: 600;
  margin: 2px 0;
}

.card-tip {
  font-size: 11px;
  color: #9ca3af;
  line-height: 1.4;
}

.sub-title {
  margin: 14px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: #303133;
}

.row-tools {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}

.hint {
  font-size: 12px;
  color: #909399;
}

.danger {
  color: #f56c6c;
  font-weight: 600;
}

.sql-block {
  margin: 0 0 12px;
  padding: 10px 12px;
  background: #f6f8fa;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 420px;
  overflow: auto;
}

.tabs :deep(.el-tabs__content) {
  overflow: visible;
}

.tab-badge {
  margin-left: 6px;
}
</style>
