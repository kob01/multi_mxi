<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, MagicStick } from '@element-plus/icons-vue'
import { useEmployee } from '../composables/useEmployee'

const { current } = useEmployee()

// 记忆桶 -> 页面顺序与列展示口径(后端 labels 只给中文名, 表头差异在这里定)。
const BUCKETS = [
  { key: 'preference', name: '偏好', desc: '明确表达过的期望与好恶, 每轮直读注入' },
  { key: 'habit', name: '习惯', desc: '重复出现的行为模式, 每轮直读注入' },
  { key: 'episode', name: '情节', desc: '带时间锚点的经历, 按语义召回近期若干条' },
  { key: 'knowledge', name: '个人知识', desc: '从经历中沉淀出的可复用要点' },
  { key: 'fact', name: '早期事实', desc: '引入分桶之前的历史记录, 仍可被召回' },
]

const SOURCE_LABELS = {
  turn: '对话提取',
  session_summary: '会话摘要',
  reflection: '经验提炼',
}

const loading = ref(false)
const reflecting = ref(false)
const activeTab = ref('profile')
const data = ref({ profile: { attributes: {}, summary: '' }, buckets: {}, graph: { nodes: [], links: [] }, stats: {} })

const activeBucket = computed(() => BUCKETS.find((b) => b.key === activeTab.value) || null)

const identityItems = computed(() =>
  Object.entries(data.value.profile?.attributes || {}).map(([key, values]) => ({
    key,
    text: (Array.isArray(values) ? values : [values]).filter(Boolean).join('、'),
  }))
)

// 波动类属性的历史值: 后端已按生效时间派生好当前值, 这里只展示被顶掉的旧观测。
const profileHistory = computed(() =>
  Object.entries(data.value.profile?.history || {})
    .map(([key, entries]) => ({
      key,
      items: (Array.isArray(entries) ? entries : [entries])
        .filter(Boolean)
        .map((entry) => (typeof entry === 'string' ? { text: entry, valid: '', recorded: '' } : {
          text: entry.value || '',
          valid: entry.valid_at ? String(entry.valid_at).slice(0, 7) : '',
          recorded: entry.recorded_at ? String(entry.recorded_at).slice(0, 10) : '',
        })),
    }))
    .filter((item) => item.items.length)
)

function rowsOf(bucket) {
  return data.value.buckets?.[bucket] || []
}

async function load() {
  loading.value = true
  try {
    const resp = await fetch(
      `/api/memory/overview?user_id=${encodeURIComponent(current.empId)}&operator=${encodeURIComponent(current.empId)}`
    )
    if (resp.ok) data.value = await resp.json()
  } catch {
    /* 后端降级时保留上一次结果, 不打断页面 */
  } finally {
    loading.value = false
  }
}

async function removeItem(bucket, row) {
  try {
    await ElMessageBox.confirm('删除后不会再被检索到, 但不会撤回已发生的对话记录。确认删除?', '删除记忆', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    const resp = await fetch(
      `/api/memory/items/${row.id}?user_id=${encodeURIComponent(current.empId)}&operator=${encodeURIComponent(current.empId)}`,
      { method: 'DELETE' }
    )
    if (!resp.ok) throw new Error(String(resp.status))
    ElMessage.success('已删除')
    await load()
  } catch {
    ElMessage.error('删除失败, 请稍后重试')
  }
}

async function clearBucket(bucket) {
  const label = bucket === 'profile' ? '用户画像' : BUCKETS.find((b) => b.key === bucket)?.name || bucket
  try {
    await ElMessageBox.confirm(`将清空「${label}」的全部记忆, 该操作不可撤销。确认继续?`, '清空记忆', {
      type: 'warning',
      confirmButtonText: '清空',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    const resp = await fetch(
      `/api/memory/bucket/${bucket}?user_id=${encodeURIComponent(current.empId)}&operator=${encodeURIComponent(current.empId)}`,
      { method: 'DELETE' }
    )
    if (!resp.ok) throw new Error(String(resp.status))
    ElMessage.success('已清空')
    await load()
  } catch {
    ElMessage.error('清空失败, 请稍后重试')
  }
}

async function reflect() {
  reflecting.value = true
  try {
    const resp = await fetch(
      `/api/memory/reflect?user_id=${encodeURIComponent(current.empId)}&operator=${encodeURIComponent(current.empId)}`,
      { method: 'POST' }
    )
    if (!resp.ok) throw new Error(String(resp.status))
    const result = await resp.json()
    const added = result.knowledge_added || 0
    const merged = result.merged || 0
    const parts = []
    if (added) parts.push(`新增 ${added} 条经验`)
    if (merged) parts.push(`合并 ${merged} 条重复`)
    ElMessage.success(parts.length ? parts.join(', ') : '记忆已是最新, 无需整理')
    await load()
  } catch {
    ElMessage.error('整理失败, 请检查服务状态')
  } finally {
    reflecting.value = false
  }
}

function fmtDate(value) {
  if (!value) return '—'
  return String(value).slice(0, 10)
}

function fmtSource(value) {
  return SOURCE_LABELS[value] || value || '—'
}

watch(() => current.empId, load)
onMounted(load)
</script>

<template>
  <div class="memory-page" v-loading="loading">
    <aside class="memory-side">
      <div class="side-title">
        <span>我的记忆</span>
        <el-button text :icon="Refresh" :loading="loading" @click="load" />
      </div>
      <p class="side-tip">
        这里存放系统从与你的对话中沉淀出的个人级记忆, 按用途分桶。内容仅你本人可见,
        可以随时删除或清空。
      </p>
      <el-menu :default-active="activeTab" class="bucket-menu" @select="(name) => (activeTab = name)">
        <el-menu-item index="profile">
          <span>用户画像</span>
          <span class="count">{{ data.stats?.profile_keys || 0 }}</span>
        </el-menu-item>
        <el-menu-item v-for="b in BUCKETS" :key="b.key" :index="b.key">
          <span>{{ b.name }}</span>
          <span class="count">{{ data.stats?.[b.key] || 0 }}</span>
        </el-menu-item>
        <el-menu-item index="graph">
          <span>个人图谱</span>
          <span class="count">{{ data.stats?.graph_entities || 0 }}</span>
        </el-menu-item>
      </el-menu>
      <el-button class="reflect-btn" type="primary" plain :icon="MagicStick" :loading="reflecting" @click="reflect">
        整理近期经历
      </el-button>
      <p class="side-foot">整理会把近期经历提炼为可复用经验, 并合并偏好/习惯里的重复条目(语义相同的只留最完整的一条)。</p>
    </aside>

    <section class="memory-main">
      <!-- 画像: 一人一条聚合结果, 覆盖式更新, 因此只能整体清 -->
      <el-card v-if="activeTab === 'profile'" shadow="never">
        <template #header>
          <div class="card-head">
            <div>
              <h3>用户画像</h3>
              <p>身份、部门、技能等属性, 每轮对话全量注入, 不做语义检索。会随时间变的值(体重/部门等)
                按生效时间取当前值, 旧值只当历史保留。</p>
            </div>
            <el-button size="small" type="danger" plain @click="clearBucket('profile')">清空画像</el-button>
          </div>
        </template>
        <el-empty v-if="!identityItems.length" description="还没有画像, 聊聊你的部门与职责吧" />
        <div v-else>
          <p class="profile-summary">{{ data.profile.summary }}</p>
          <el-descriptions :column="1" border>
            <el-descriptions-item v-for="item in identityItems" :key="item.key" :label="item.key">
              {{ item.text }}
            </el-descriptions-item>
          </el-descriptions>
          <div v-if="profileHistory.length" class="profile-history">
            <p class="history-tip">以下属性有过往值: 当前值按生效时间取最新, 旧值不会被一句历史陈述顶掉。</p>
            <el-collapse>
              <el-collapse-item
                v-for="entry in profileHistory"
                :key="entry.key"
                :title="`${entry.key}：${entry.items.length} 条历史值`"
              >
                <ul class="history-list">
                  <li v-for="(item, index) in entry.items" :key="index">
                    <span>{{ item.text }}</span>
                    <span v-if="item.valid" class="history-meta">生效 {{ item.valid }}</span>
                    <span v-if="item.recorded" class="history-meta">记录于 {{ item.recorded }}</span>
                  </li>
                </ul>
              </el-collapse-item>
            </el-collapse>
          </div>
        </div>
      </el-card>

      <!-- 图谱: 一跳子图, 用列表呈现节点与关系, 避免为这一页引入图渲染依赖 -->
      <el-card v-else-if="activeTab === 'graph'" shadow="never">
        <template #header>
          <div class="card-head">
            <div>
              <h3>个人图谱</h3>
              <p>以你为中心的实体关系, 用于召回"相关联的记忆"。</p>
            </div>
          </div>
        </template>
        <el-empty v-if="!data.graph?.nodes?.length" description="图里还没有实体" />
        <div v-else class="graph-body">
          <div class="graph-col">
            <h4>实体（{{ data.graph.nodes.length }}）</h4>
            <el-table :data="data.graph.nodes" size="small" max-height="360">
              <el-table-column prop="name" label="名称" />
              <el-table-column prop="type" label="类型" width="120" />
            </el-table>
          </div>
          <div class="graph-col">
            <h4>关系（{{ data.graph.links.length }}）</h4>
            <el-table :data="data.graph.links" size="small" max-height="360">
              <el-table-column prop="src" label="主体" />
              <el-table-column prop="relation" label="关系" width="110" />
              <el-table-column prop="dst" label="客体" />
              <el-table-column prop="state" label="状态" width="150">
                <template #default="{ row }">
                  <el-tag v-if="row.state === 'expired'" size="small" type="info" effect="plain">
                    已失效{{ row.invalid_at ? ' · ' + row.invalid_at : '' }}
                  </el-tag>
                  <el-tag v-else size="small" type="success" effect="plain">
                    当前{{ row.valid_at ? ' · ' + row.valid_at : '' }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-card>

      <el-card v-else shadow="never">
        <template #header>
          <div class="card-head">
            <div>
              <h3>{{ activeBucket?.name }}</h3>
              <p>{{ activeBucket?.desc }}</p>
            </div>
            <el-button size="small" type="danger" plain @click="clearBucket(activeTab)">清空该桶</el-button>
          </div>
        </template>
        <el-empty v-if="!rowsOf(activeTab).length" description="这一桶还是空的" />
        <el-table v-else :data="rowsOf(activeTab)" size="small" max-height="560">
          <el-table-column v-if="activeTab === 'episode'" prop="occurred_at" label="发生" width="110">
            <template #default="{ row }">{{ fmtDate(row.occurred_at) }}</template>
          </el-table-column>
          <el-table-column v-if="activeTab === 'episode'" prop="title" label="标题" width="150" show-overflow-tooltip />
          <el-table-column prop="content" label="内容" min-width="320" show-overflow-tooltip />
          <el-table-column prop="source" label="来源" width="120">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ fmtSource(row.source) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="记录于" width="110">
            <template #default="{ row }">{{ fmtDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button size="small" type="danger" link @click="removeItem(activeTab, row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </section>
  </div>
</template>

<style scoped>
.memory-page {
  display: flex;
  flex: 1;
  min-height: 0;
  gap: 16px;
  padding: 16px;
  overflow: hidden;
}

.memory-side {
  width: 260px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  background: #fff;
  border-radius: 8px;
  border: 1px solid #ebeef5;
  overflow: auto;
}

.side-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 16px;
  font-weight: 600;
}

.side-tip,
.side-foot {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: #909399;
}

.bucket-menu {
  border-right: none;
}

.bucket-menu :deep(.el-menu-item) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-right: 8px;
}

.count {
  font-size: 12px;
  color: #909399;
}

.reflect-btn {
  width: 100%;
}

.memory-main {
  flex: 1;
  min-width: 0;
  overflow: auto;
}

.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.card-head h3 {
  margin: 0;
  font-size: 16px;
}

.card-head p {
  margin: 4px 0 0;
  font-size: 12px;
  color: #909399;
}

.profile-summary {
  margin: 0 0 12px;
  padding: 10px 12px;
  background: #f5f7fa;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.7;
  color: #606266;
}

.profile-history {
  margin-top: 14px;
}

.history-tip {
  margin: 0 0 6px;
  font-size: 12px;
  line-height: 1.6;
  color: #909399;
}

.history-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.9;
  color: #606266;
}

.history-meta {
  margin-left: 8px;
  font-size: 12px;
  color: #a8abb2;
}

.graph-body {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
}

.graph-col {
  flex: 1;
  min-width: 280px;
}

.graph-col h4 {
  margin: 0 0 8px;
  font-size: 13px;
  color: #606266;
}
</style>
