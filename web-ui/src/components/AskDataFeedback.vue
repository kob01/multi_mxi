<script setup>
// 智能问数的反馈条: 点赞 / 点踩 / 我改的口径。
//
// 这三个按钮是 badcase 归因的唯一人工入口: 后端按留痕里的错误码、候选分数与
// verdict 做规则归因, 再产出"补词草案/评测集草案"。所以这里必须带上 query_id
// (它在 payload 里, 由 AskData_Agent 写完留痕后回传), 没有它这条反馈就无处归因。
import { ref, computed } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  queryId: { type: String, default: '' },
  payload: { type: Object, default: () => ({}) },
  userId: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
})

const sent = ref('')
const busy = ref(false)
const note = ref('')
const panelOpen = ref(false)

const metrics = computed(() => (props.payload.metrics || []).slice(0, 4))
const chosenMetric = ref('')

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
    if (kind === 'rewritten' && chosenMetric.value) {
      // 只带"用户改选的口径 id": 真正的 DSL 修正由后端在下一轮问数里自然产生。
      body.corrected_dsl = { metrics: [{ id: chosenMetric.value }] }
    }
    const resp = await fetch('/api/askdata/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await resp.json().catch(() => ({}))
    if (!resp.ok) {
      ElMessage.error(data.detail || '反馈提交失败')
      return
    }
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

function openRewrite() {
  panelOpen.value = true
  chosenMetric.value = ''
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
    </template>
    <span v-else class="done">已收到反馈，会进入 badcase 归因(生成的口径草案需人工核对后生效)</span>

    <el-dialog v-model="panelOpen" title="这个数应该按哪个口径算？" width="420px">
      <div class="row">
        <span class="label">改选指标</span>
        <el-select v-model="chosenMetric" size="small" placeholder="从刚才的候选里选" clearable>
          <el-option v-for="m in metrics" :key="m.id" :value="m.id" :label="`${m.label}（${m.id}）`" />
        </el-select>
      </div>
      <div class="row">
        <span class="label">补充说明</span>
        <el-input v-model="note" size="small" type="textarea" :rows="2"
          placeholder="例如：应该只算已打款的，不含待审" />
      </div>
      <template #footer>
        <el-button size="small" @click="panelOpen = false">取消</el-button>
        <el-button size="small" type="primary" :loading="busy" @click="submit('rewritten')">
          提交
        </el-button>
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
</style>
