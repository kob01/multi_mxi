<script setup>
import { ref, reactive, onMounted, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { BubbleList, XSender } from 'vue-element-plus-x'
import { useEmployee } from '../composables/useEmployee'

const { current } = useEmployee()

// 每次进入页面生成一个会话 ID（与原实现一致）
const sessionId = 'web-' + Math.random().toString(36).slice(2, 10)

const ROUTE_LABELS = {
  assistant_kb: '知识库',
  mcp_tool: 'MCP工具',
  a2a_agent: '专业智能体',
  direct: '直答',
}
const ROUTE_TAG_TYPES = {
  assistant_kb: 'success',
  mcp_tool: 'warning',
  a2a_agent: 'primary',
  direct: 'info',
}

const senderRef = ref()
const sending = ref(false)
const listRef = ref()
let nextKey = 0

const messages = reactive([])

function pushMessage(msg) {
  messages.push({ key: ++nextKey, ...msg })
}

// 欢迎语
onMounted(() => {
  pushMessage({
    role: 'ai',
    placement: 'start',
    variant: 'filled',
    shape: 'corner',
    content:
      '你好，我是企业智能助手马小i。可以问我制度政策（如"年假有几天"），也可以直接说"我要报销""帮我开在职证明"。',
    headerTag: { text: '马小i', type: 'info' },
  })
})

async function handleSubmit() {
  if (sending.value) return
  const text = (senderRef.value?.getModelValue()?.text || '').trim()
  if (!text) return
  senderRef.value?.clear()

  pushMessage({ role: 'user', placement: 'end', variant: 'outlined', shape: 'corner', content: text })
  sending.value = true
  const pendingIdx = messages.length
  pushMessage({
    role: 'ai',
    placement: 'start',
    variant: 'filled',
    shape: 'corner',
    loading: true,
    content: '',
    headerTag: { text: '马小i', type: 'info' },
  })

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sessionId,
        user_id: current.empId,
        role: current.role,
        department: current.department,
        message: text,
      }),
    })
    const data = await resp.json()
    const msg = messages[pendingIdx]
    msg.loading = false
    if (!resp.ok) {
      msg.content = '服务异常: ' + (data.detail || resp.status)
      msg.headerTag = { text: '错误', type: 'danger' }
      return
    }
    msg.content = data.answer
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
  } catch (e) {
    const msg = messages[pendingIdx]
    msg.loading = false
    msg.content = '网络错误: ' + e.message
    msg.headerTag = { text: '错误', type: 'danger' }
  } finally {
    sending.value = false
    nextTick(() => listRef.value?.scrollToBottom(true))
    senderRef.value?.focus?.()
  }
}
</script>

<template>
  <div class="chat-page">
    <div class="chat-card">
      <BubbleList
        ref="listRef"
        :list="messages"
        max-height="100%"
        class="bubble-list"
      >
        <template #header="{ item }">
          <el-tag
            v-if="item.headerTag"
            size="small"
            :type="item.headerTag.type"
            effect="light"
            round
          >
            {{ item.headerTag.text }}
          </el-tag>
        </template>
        <template #footer="{ item }">
          <div v-if="item.cites && item.cites.length" class="cites">
            📎 参考来源：{{ item.cites.join(' ; ') }}
          </div>
        </template>
      </BubbleList>

      <div class="sender-wrap">
        <XSender
          ref="senderRef"
          placeholder="请输入消息，回车发送…"
          :loading="sending"
          clearable
          @submit="handleSubmit"
        />
      </div>
    </div>
    <div class="hint">Assistant 统一入口 · 知识库 / MCP 工具 / A2A 专业智能体分层调度</div>
  </div>
</template>

<style scoped>
.chat-page {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 16px 20px 8px;
  max-width: 960px;
  width: 100%;
  margin: 0 auto;
  min-height: 0;
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
.hint {
  font-size: 12px;
  color: #888;
  text-align: center;
  padding: 8px 0;
  flex-shrink: 0;
}
</style>
