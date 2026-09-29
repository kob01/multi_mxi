<script setup>
import { ref, reactive, computed, watch, onMounted, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Plus } from '@element-plus/icons-vue'
import { BubbleList, XSender } from 'vue-element-plus-x'
import MarkdownView from '../components/MarkdownView.vue'
import { useEmployee } from '../composables/useEmployee'

const { current } = useEmployee()

// ---------- 会话与断点续传的本地持久化 ----------
// session_id 跨刷新/重进页面保持稳定, 历史记录凭它从后端回看;
// active_run_id + last_event_id 让页面刷新后能对未完成的流式回答续传。
const LS_SESSION = 'mxi_session_id'
const LS_RUN = 'mxi_active_run_id'
const LS_EVENT = 'mxi_last_event_id'

function newSessionId() {
  return 'web-' + Math.random().toString(36).slice(2, 10)
}
function getSessionId() {
  let id = localStorage.getItem(LS_SESSION)
  if (!id) {
    id = newSessionId()
    localStorage.setItem(LS_SESSION, id)
  }
  return id
}
function clearRun() {
  localStorage.removeItem(LS_RUN)
  localStorage.removeItem(LS_EVENT)
}

const sessionId = ref(getSessionId())

const ROUTE_LABELS = {
  assistant_kb: '知识库',
  mcp_tool: 'MCP工具',
  a2a_agent: '专业智能体',
  direct: '直答',
  multi_task: '多任务并行',
}
const ROUTE_TAG_TYPES = {
  assistant_kb: 'success',
  mcp_tool: 'warning',
  a2a_agent: 'primary',
  direct: 'info',
  multi_task: 'primary',
}

// ---------- 多任务逐项进度的解析 ----------
// 后端 subtask 阶段事件文本形状: ``[i/n] 子问题 → 路由标签…`` 开头,
// ``... → 路由标签已完成/未完成`` 结尾; planning 阶段给总件数。
const SUBTASK_RE = /^\[(\d+)\/(\d+)\]\s*(.+?)\s*→\s*(.+?)(已完成|未完成|…)$/
const SUBTASK_STATE_ICONS = { waiting: '○', running: '◐', done: '✔', failed: '✖' }

function subtaskLabel(route, target) {
  const base = ROUTE_LABELS[route] || route || ''
  return target && (route === 'mcp_tool' || route === 'a2a_agent') ? `${base}·${target}` : base
}

// 子任务清单只是进度的第二双眼睛(正文已含分节结果), 解不了就退回单行 status 文本。
function trackSubTasks(msg, ev) {
  if (ev.stage === 'planning') {
    const m = /识别到 (\d+) 件事/.exec(ev.text || '')
    if (m && !(msg.subTasks && msg.subTasks.length)) {
      msg.subTasks = Array.from({ length: Number(m[1]) }, (_, i) => ({
        index: i + 1, query: '', label: '', state: 'waiting',
      }))
    }
    return
  }
  if (ev.stage !== 'subtask') return
  const m = SUBTASK_RE.exec(ev.text || '')
  if (!m) return
  if (!msg.subTasks) msg.subTasks = []
  const idx = Number(m[1]) - 1
  if (!msg.subTasks[idx]) {
    msg.subTasks[idx] = { index: idx + 1, query: '', label: '', state: 'waiting' }
  }
  msg.subTasks[idx] = {
    index: idx + 1,
    query: m[2] || msg.subTasks[idx].query,
    label: m[3] || msg.subTasks[idx].label,
    state: m[4] === '已完成' ? 'done' : m[4] === '未完成' ? 'failed' : 'running',
  }
}

// result 事件里的 metadata.subtasks 是成败的单一事实源, 用它覆盖 SSE 期间推进的状态。
function applySubTaskResults(msg, ev) {
  const list = (ev.metadata && ev.metadata.subtasks) || []
  if (ev.route !== 'multi_task' || !list.length) {
    if (ev.route !== 'multi_task') msg.subTasks = []
    return
  }
  msg.subTasks = list.map((s) => ({
    index: Number(s.index || 0) + 1,
    query: s.query || '',
    label: subtaskLabel(s.route, s.target),
    state: s.ok ? 'done' : 'failed',
    error: s.error || '',
  }))
}

const senderRef = ref()
const sending = ref(false)
const thinkingOn = ref(true) // 深度思考开关(默认开, 与后端 LLM_THINKING_ENABLED 同向)
const listRef = ref()
let nextKey = 0

const messages = reactive([])

// ---------- 左侧会话列表 ----------
const sessions = ref([])
const sessionsLoading = ref(false)

async function loadSessions() {
  sessionsLoading.value = true
  try {
    const resp = await fetch(`/api/sessions?user_id=${encodeURIComponent(current.empId)}`)
    sessions.value = resp.ok ? await resp.json() : []
  } catch {
    sessions.value = []
  } finally {
    sessionsLoading.value = false
  }
}

function fmtTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const pad = (n) => String(n).padStart(2, '0')
  const sameYear = d.getFullYear() === new Date().getFullYear()
  const date = `${sameYear ? '' : d.getFullYear() + '-'}${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  return `${date} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function switchSession(id) {
  if (id === sessionId.value || sending.value) return
  sessionId.value = id
  localStorage.setItem(LS_SESSION, id)
  clearRun() // 换会话时丢弃旧会话未完成的续传指针
  await renderSession(id)
}

async function createSession() {
  if (sending.value) return
  const id = newSessionId()
  sessionId.value = id
  localStorage.setItem(LS_SESSION, id)
  clearRun()
  await renderSession(id)
}

async function removeSession(s) {
  try {
    await ElMessageBox.confirm(`删除会话「${s.title}」及其全部记录？不可恢复。`, '删除会话', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await fetch(`/api/sessions/${encodeURIComponent(s.session_id)}`, { method: 'DELETE' })
  } catch {
    /* 后端降级时本地照常移除 */
  }
  sessions.value = sessions.value.filter((x) => x.session_id !== s.session_id)
  if (s.session_id === sessionId.value) await createSession()
  ElMessage.success('已删除')
}

function pushMessage(msg) {
  messages.push({ key: ++nextKey, ...msg })
  return messages[messages.length - 1]
}

function makeAiPending() {
  return pushMessage({
    role: 'ai',
    placement: 'start',
    variant: 'filled',
    shape: 'corner',
    loading: true,
    content: '',
    status: '',
    thinking: '',
    thinkingOpen: true,
    subTasks: [],
    headerTag: { text: '马小i', type: 'info' },
  })
}

function pushWelcome() {
  pushMessage({
    role: 'ai',
    placement: 'start',
    variant: 'filled',
    shape: 'corner',
    content:
      '你好，我是企业智能助手马小i。可以问我制度政策（如"年假有几天"），也可以直接说"我要报销""帮我开在职证明"；管理角色还能让我"生成本周经营周报""帮我审一下这份合同"。说"把这份数据导出成 Word/Excel/PDF"，我会生成可下载的文件。',
    headerTag: { text: '马小i', type: 'info' },
  })
}

function applyRouteTag(msg, data) {
  msg.headerTag = {
    text:
      data.route === 'a2a_agent'
        ? `${ROUTE_LABELS.a2a_agent}·${data.target || ''}`
        : ROUTE_LABELS[data.route] || data.route,
    type: ROUTE_TAG_TYPES[data.route] || 'info',
  }
  msg.route = data.route
  const docs = (data.metadata && data.metadata.docs) || []
  const seen = new Set()
  msg.cites = docs
    .filter((d) => {
      if (seen.has(d.doc_key)) return false
      seen.add(d.doc_key)
      return true
    })
    .map((d) => {
      let s = '《' + d.title + '》'
      if (d.section) s += ' ' + d.section
      if (d.page_no > 0) s += ' 第' + d.page_no + '页'
      if (d.tags && d.tags.length) s += ' [' + d.tags.join(', ') + ']'
      return s
    })
}

// ---------- SSE 流消费(POST 发起 / GET 断点重连共用) ----------
async function consumeStream(resp, msg) {
  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let bufText = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    bufText += decoder.decode(value, { stream: true })
    let idx
    while ((idx = bufText.indexOf('\n\n')) >= 0) {
      const frame = bufText.slice(0, idx)
      bufText = bufText.slice(idx + 2)
      handleFrame(frame, msg)
    }
  }
  if (bufText.trim()) handleFrame(bufText, msg)
}

function handleFrame(frame, msg) {
  let eventId = null
  let data = null
  for (const line of frame.split('\n')) {
    if (line.startsWith('id:')) eventId = parseInt(line.slice(3).trim(), 10)
    else if (line.startsWith('data:')) data = JSON.parse(line.slice(5).trim())
  }
  if (data && Number.isFinite(eventId)) localStorage.setItem(LS_EVENT, String(eventId))
  if (data) handleEvent(data, msg)
}

function handleEvent(ev, msg) {
  switch (ev.type) {
    case 'run':
      localStorage.setItem(LS_RUN, ev.run_id)
      break
    case 'status':
      msg.status = ev.text || ''
      trackSubTasks(msg, ev)
      break
    case 'think':
      msg.loading = false
      msg.thinking += ev.delta || ''
      break
    case 'token':
      msg.loading = false
      msg.thinkingOpen = false
      msg.content += ev.delta || ''
      break
    case 'result':
      msg.loading = false
      msg.thinkingOpen = false
      msg.status = ''
      if (!msg.content) msg.content = ev.answer || '' // 降级路径: 一次性全文补渲染
      if (ev.thinking_text && !msg.thinking) msg.thinking = ev.thinking_text
      applySubTaskResults(msg, ev)
      applyRouteTag(msg, ev)
      break
    case 'error':
      msg.loading = false
      msg.status = ''
      msg.content = msg.content || '服务异常: ' + (ev.message || '未知错误')
      msg.headerTag = { text: '错误', type: 'danger' }
      break
    case 'done':
      msg.loading = false
      msg.status = ''
      msg.thinkingOpen = false
      clearRun()
      break
  }
}

async function sendStream(body, msg) {
  let completed = false
  try {
    const resp = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}))
      msg.loading = false
      msg.content = '服务异常: ' + (data.detail || resp.status)
      msg.headerTag = { text: '错误', type: 'danger' }
      return
    }
    await consumeStream(resp, msg)
    completed = !localStorage.getItem(LS_RUN)
  } catch (e) {
    // 连接断开不清 run_id: 刷新后仍可凭 Last-Event-ID 续流
    if (msg.content) return
    msg.loading = false
    msg.content = '网络错误: ' + e.message
    msg.headerTag = { text: '错误', type: 'danger' }
  } finally {
    sending.value = false
    if (completed) refreshAfterTurn()
    nextTick(() => listRef.value?.scrollToBottom(true))
    senderRef.value?.focus?.()
  }
}

// 首轮问答会把会话写入后端: 结束后补刷新列表(新会话/改标题需要)
async function refreshAfterTurn() {
  const known = sessions.value.some((s) => s.session_id === sessionId.value)
  if (!known) await loadSessions()
}

// ---------- 历史回填 ----------
function historyToMessage(m) {
  const base =
    m.role === 'user'
      ? { role: 'user', placement: 'end', variant: 'outlined', shape: 'corner', content: m.content }
      : {
        role: 'ai',
        placement: 'start',
        variant: 'filled',
        shape: 'corner',
        content: m.content,
        thinking: m.thinking || '',
        thinkingOpen: false,
        subTasks: [],
        headerTag: { text: '马小i', type: 'info' },
      }
  if (m.role !== 'user') {
    applyRouteTag(base, {
      route: m.route, target: m.target, metadata: { docs: m.docs_meta || [] }, artifacts: m.artifacts || [],
    })
  }
  return base
}

async function loadHistory(id) {
  try {
    const resp = await fetch(`/api/sessions/${encodeURIComponent(id)}/messages`)
    return resp.ok ? await resp.json() : []
  } catch {
    return []
  }
}

// 切换/新建会话后重绘主区
async function renderSession(id) {
  messages.splice(0, messages.length)
  const history = await loadHistory(id)
  if (history.length) {
    history.forEach((m) => pushMessage(historyToMessage(m)))
  } else {
    pushWelcome()
  }
  nextTick(() => listRef.value?.scrollToBottom(true))
}

// ---------- 跨刷新续流 ----------
async function resumeIfNeeded() {
  const runId = localStorage.getItem(LS_RUN)
  if (!runId) return
  const lastId = parseInt(localStorage.getItem(LS_EVENT) || '0', 10)
  const msg = makeAiPending()
  msg.status = '正在恢复上一轮回答…'
  sending.value = true
  try {
    const resp = await fetch(`/api/chat/stream/${encodeURIComponent(runId)}`, {
      headers: { 'Last-Event-ID': String(lastId) },
    })
    if (!resp.ok) {
      // run 已过期/服务已重启: 丢弃占位气泡, 用已落库的历史兜底
      messages.splice(messages.indexOf(msg), 1)
      clearRun()
      await renderSession(sessionId.value)
      return
    }
    await consumeStream(resp, msg)
  } catch {
    /* 续流再次断开: 保留 run_id, 下次刷新继续尝试 */
  } finally {
    sending.value = false
    if (msg.content) msg.loading = false
    nextTick(() => listRef.value?.scrollToBottom(true))
  }
}

onMounted(async () => {
  await loadSessions()
  await renderSession(sessionId.value)
  await resumeIfNeeded()
})

// 切换员工: 会话列表按用户隔离, 主区回到该员工自己的当前会话
watch(
  () => current.empId,
  async () => {
    await loadSessions()
    const owns = sessions.value.some((s) => s.session_id === sessionId.value)
    if (!owns && sessions.value.length) {
      sessionId.value = sessions.value[0].session_id
      localStorage.setItem(LS_SESSION, sessionId.value)
    } else if (!owns) {
      sessionId.value = newSessionId()
      localStorage.setItem(LS_SESSION, sessionId.value)
    }
    clearRun()
    await renderSession(sessionId.value)
  }
)

async function handleSubmit() {
  if (sending.value) return
  const text = (senderRef.value?.getModelValue()?.text || '').trim()
  if (!text) return
  senderRef.value?.clear()
  await sendText(text)
}

// 正文发送(对话页与"从工坊带一需求回来"共用同一条链路)
async function sendText(text) {
  pushMessage({ role: 'user', placement: 'end', variant: 'outlined', shape: 'corner', content: text })
  sending.value = true
  const msg = makeAiPending()
  await sendStream({
    session_id: sessionId.value,
    user_id: current.empId,
    role: current.role,
    department: current.department,
    message: text,
    thinking: thinkingOn.value,
  }, msg)
}

const currentSessionId = computed(() => sessionId.value)
</script>

<template>
  <div class="chat-page">
    <!-- 左侧: 会话列表(后端 /api/sessions, 支持切换/删除/新建) -->
    <aside class="session-side">
      <el-button class="new-btn" type="primary" plain :icon="Plus" @click="createSession">
        新对话
      </el-button>
      <div v-loading="sessionsLoading" class="session-list">
        <div v-for="s in sessions" :key="s.session_id" class="session-item"
          :class="{ active: s.session_id === currentSessionId }" @click="switchSession(s.session_id)">
          <div class="s-main">
            <div class="s-title">{{ s.title }}</div>
            <div class="s-time">{{ fmtTime(s.updated_at) }}</div>
          </div>
          <el-icon class="s-del" title="删除会话" @click.stop="removeSession(s)">
            <Delete />
          </el-icon>
        </div>
        <el-empty v-if="!sessionsLoading && !sessions.length" description="暂无历史会话" :image-size="60" />
      </div>
    </aside>

    <!-- 右侧: 当前会话 -->
    <div class="chat-main">
      <div class="chat-card">
        <BubbleList ref="listRef" :list="messages" max-height="100%" class="bubble-list">
          <template #header="{ item }">
            <el-tag v-if="item.headerTag" size="small" :type="item.headerTag.type" effect="light" round>
              {{ item.headerTag.text }}
            </el-tag>
          </template>
          <!-- 正文: AI 回答按 Markdown 渲染(表格/代码块/加粗等), 用户消息保持纯文本 -->
          <template #content="{ item }">
            <MarkdownView v-if="item.role !== 'user'" :content="item.content || ''" />
            <div v-else class="user-text">{{ item.content }}</div>
          </template>
          <template #footer="{ item }">
            <div v-if="item.thinking" class="thinking">
              <div class="thinking-head" @click="item.thinkingOpen = !item.thinkingOpen">
                💡 思考过程{{ item.thinkingOpen ? '（点击收起）' : '（点击展开）' }}
              </div>
              <div v-show="item.thinkingOpen" class="thinking-body">{{ item.thinking }}</div>
            </div>
            <!-- 多任务并行: 逐项进度清单(后端 status 事件推进, result 事件定稿) -->
            <div v-if="item.subTasks && item.subTasks.length" class="subtask-list">
              <div v-for="t in item.subTasks" :key="t.index" class="subtask-item">
                <span class="subtask-state" :class="'st-' + t.state">
                  {{ SUBTASK_STATE_ICONS[t.state] || '○' }}
                </span>
                <span class="subtask-query">{{ t.query || '第 ' + t.index + ' 项' }}</span>
                <span v-if="t.label" class="subtask-label">{{ t.label }}</span>
                <span v-if="t.error" class="subtask-error">{{ t.error }}</span>
              </div>
            </div>
            <div v-if="item.status" class="status-line">{{ item.status }}</div>
            <div v-if="item.cites && item.cites.length" class="cites">
              📎 参考来源：{{ item.cites.join(' ; ') }}
            </div>
          </template>
        </BubbleList>

        <div class="sender-wrap">
          <div class="sender-toolbar">
            <el-switch v-model="thinkingOn" size="small" active-text="深度思考" />
          </div>
          <XSender ref="senderRef" placeholder="请输入消息，回车发送…" :loading="sending" clearable @submit="handleSubmit" />
        </div>
      </div>
      <div class="hint">Assistant 统一入口 · SSE 流式输出支持刷新断点续传</div>
    </div>
  </div>
</template>

<style scoped>
.chat-page {
  flex: 1;
  display: flex;
  min-height: 0;
  min-width: 0;
  padding: 16px 20px 8px;
  gap: 14px;
}

.session-side {
  width: 230px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid #e3e6ee;
  border-radius: 12px;
  padding: 12px 8px;
  min-height: 0;
}

.new-btn {
  width: 100%;
  margin-bottom: 10px;
}

.session-list {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}

.session-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 8px;
  border-radius: 8px;
  cursor: pointer;
}

.session-item:hover {
  background: #f2f5fb;
}

.session-item.active {
  background: #e9efff;
}

.s-main {
  flex: 1;
  min-width: 0;
}

.s-title {
  font-size: 13px;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.s-time {
  font-size: 11px;
  color: #98a0b3;
  margin-top: 2px;
}

.s-del {
  visibility: hidden;
  color: #c0c4cc;
  flex-shrink: 0;
}

.session-item:hover .s-del {
  visibility: visible;
}

.s-del:hover {
  color: #f56c6c;
}

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  max-width: 960px;
  margin: 0 auto;
}

.chat-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #fff;
  border: 1px solid #e3e6ee;
  border-radius: 12px;
  padding: 16px;
}

.bubble-list {
  flex: 1;
  min-height: 0;
}

.user-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
}

.thinking {
  margin-top: 6px;
  font-size: 12px;
  color: #7a7f8a;
  background: #f7f8fb;
  border: 1px dashed #dde1ea;
  border-radius: 8px;
  max-width: 640px;
}

.thinking-head {
  padding: 4px 8px;
  cursor: pointer;
  user-select: none;
  color: #98a0b3;
}

.thinking-body {
  padding: 0 8px 6px;
  white-space: pre-wrap;
  line-height: 1.6;
  max-height: 220px;
  overflow-y: auto;
}

.status-line {
  font-size: 12px;
  color: #409eff;
  margin-top: 6px;
}

.subtask-list {
  margin-top: 6px;
  padding: 6px 8px;
  border-radius: 8px;
  background: #f7f9fc;
  font-size: 12px;
  line-height: 1.7;
  color: #5b6479;
}

.subtask-item {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
}

.subtask-state {
  width: 14px;
  flex-shrink: 0;
}

.st-done {
  color: #67c23a;
}

.st-failed {
  color: #f56c6c;
}

.st-running {
  color: #409eff;
}

.subtask-query {
  color: #303133;
}

.subtask-label {
  padding: 0 6px;
  border-radius: 6px;
  background: #eef1f6;
  color: #8892a6;
  font-size: 11px;
}

.subtask-error {
  color: #f56c6c;
  font-size: 11px;
}

.cites {
  font-size: 11px;
  color: #888;
  margin-top: 6px;
  line-height: 1.5;
}

.sender-wrap {
  margin-top: 12px;
  border-top: 1px solid #eef1f6;
  padding-top: 12px;
}

.sender-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 6px;
}

.hint {
  font-size: 12px;
  color: #888;
  text-align: center;
  padding: 8px 0;
  flex-shrink: 0;
}
</style>
