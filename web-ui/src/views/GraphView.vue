<script setup>
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Plus, Delete } from '@element-plus/icons-vue'
import { Graph } from '@antv/g6'
import { useEmployee } from '../composables/useEmployee'

const { current } = useEmployee()

// 文档/实体配色：文档按模态区分，实体按类型区分
const DOC_COLORS = { text: '#1f3a93', video_transcript: '#b8860b', image: '#8e44ad' }
const ENTITY_COLORS = {
  person: '#e74c3c',
  department: '#16a085',
  system: '#2980b9',
  document: '#7f8c8d',
  policy: '#d35400',
  term: '#27ae60',
  other: '#95a5a6',
}

const loading = ref(false)
const building = ref(false)
const enabled = ref(true)
const truncated = ref(false)
const stats = ref({ docs: 0, entities: 0 })
const graphEl = ref()

let graph = null
// 累积的原始数据（doc/entity 节点 + MENTIONS/KG_REL 边），聚焦展开时增量合并
const nodeMap = new Map()
const edgeMap = new Map()

function authParams() {
  return { user_id: current.empId, role: current.role, department: current.department }
}

async function fetchGraph(focus) {
  const p = new URLSearchParams(authParams())
  if (focus) p.set('focus', focus)
  const resp = await fetch(`/api/kg/graph?${p.toString()}`)
  if (!resp.ok) throw new Error(resp.status)
  return await resp.json()
}

function nodeStyle(data) {
  if (data.group === 'doc') {
    return { fill: DOC_COLORS[data.modality] || DOC_COLORS.text, size: 46 }
  }
  return { fill: ENTITY_COLORS[data.type] || ENTITY_COLORS.other, size: 22 }
}

function toG6Node(n) {
  const style = nodeStyle(n)
  return {
    id: n.id,
    data: { ...n },
    style: {
      fill: style.fill,
      size: style.size,
      labelText: n.label,
      labelFill: '#303133',
      labelFontSize: n.group === 'doc' ? 12 : 10,
      labelPlacement: 'bottom',
      stroke: '#fff',
      lineWidth: 1.5,
    },
  }
}

function toG6Edge(e) {
  const rel = e.type === 'KG_REL'
  return {
    id: `${e.source}->${e.target}#${e.label || ''}`,
    source: e.source,
    target: e.target,
    data: { type: e.type, label: e.label },
    style: {
      stroke: rel ? '#909399' : '#c0cdf0',
      lineWidth: rel ? 1.4 : 1,
      labelText: e.label || '',
      labelFontSize: 9,
      labelFill: '#909399',
      endArrow: rel,
    },
  }
}

function mergeGraph(data) {
  for (const n of data.nodes || []) {
    if (!nodeMap.has(n.id)) nodeMap.set(n.id, n)
  }
  for (const e of data.edges || []) {
    const key = `${e.source}->${e.target}#${e.label || ''}`
    if (!edgeMap.has(key)) edgeMap.set(key, e)
  }
}

function render() {
  const nodes = [...nodeMap.values()].map(toG6Node)
  const edges = [...edgeMap.values()].map(toG6Edge)
  stats.value = {
    docs: nodes.filter((n) => n.data.group === 'doc').length,
    entities: nodes.filter((n) => n.data.group === 'entity').length,
  }
  if (!graph) return
  graph.setData({ nodes, edges })
  graph.render()
}

function ensureGraph() {
  if (graph || !graphEl.value) return
  graph = new Graph({
    container: graphEl.value,
    autoResize: true,
    autoFit: 'view',
    padding: 32,
    data: { nodes: [], edges: [] },
    node: {
      type: 'circle',
      state: { selected: { lineWidth: 3, stroke: '#ffcc00' } },
    },
    edge: { type: 'line' },
    layout: {
      type: 'd3-force',
      preventOverlap: true,
      link: { distance: 130 },
      collide: { radius: 34 },
    },
    behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element'],
  })
  // 点击文档节点：拉取其邻域子图并增量合并进当前视图
  graph.on('node:click', (evt) => {
    const id = evt.target?.id
    const node = id ? nodeMap.get(id) : null
    if (node && node.group === 'doc') expandFocus(node.doc_key)
  })
}

async function expandFocus(docKey) {
  try {
    const data = await fetchGraph(docKey)
    mergeGraph(data)
    render()
    ElMessage.success(`已展开「${docKey}」的关联邻域`)
  } catch (e) {
    ElMessage.error('展开失败: ' + e.message)
  }
}

async function loadGraph() {
  loading.value = true
  try {
    nodeMap.clear()
    edgeMap.clear()
    const data = await fetchGraph(null)
    enabled.value = data.enabled !== false
    truncated.value = !!data.truncated
    mergeGraph(data)
    ensureGraph()
    render()
    if (!enabled.value) ElMessage.warning('文档知识图谱未启用（DOC_KG_ENABLED=false）')
  } catch (e) {
    ElMessage.error('图谱加载失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

function resetView() {
  nodeMap.clear()
  edgeMap.clear()
  loadGraph()
}

async function rebuildAll() {
  building.value = true
  try {
    const resp = await fetch(
      `/api/kg/rebuild-all?operator=${encodeURIComponent(current.empId)}`,
      { method: 'POST' },
    )
    const data = await resp.json()
    if (!resp.ok) throw new Error(data.detail || resp.status)
    ElMessage.success(data.message || '回填已在后台启动')
  } catch (e) {
    ElMessage.error('触发重建失败: ' + e.message)
  } finally {
    building.value = false
  }
}

onMounted(async () => {
  await nextTick()
  await loadGraph()
})

onBeforeUnmount(() => {
  graph?.destroy()
  graph = null
})
</script>

<template>
  <div class="kg-page" v-loading="loading">
    <el-card shadow="never" class="kg-toolbar">
      <div class="bar">
        <span class="title">文档知识图谱</span>
        <span class="hint">
          {{ stats.docs }} 篇文档 · {{ stats.entities }} 个实体 · 点击文档节点展开关联
        </span>
        <div class="actions">
          <el-tag v-if="truncated" size="small" type="warning" effect="plain">节点已达上限（已截断）</el-tag>
          <el-tag v-if="!enabled" size="small" type="danger" effect="plain">未启用</el-tag>
          <el-button :icon="Refresh" size="small" :loading="loading" @click="resetView">刷新</el-button>
          <el-button :icon="Plus" size="small" type="primary" plain :loading="building" @click="rebuildAll">
            回填存量文档
          </el-button>
        </div>
      </div>
    </el-card>

    <div class="kg-canvas-wrap">
      <div ref="graphEl" class="kg-canvas"></div>
      <el-empty
        v-if="!loading && stats.docs === 0 && stats.entities === 0"
        class="kg-empty"
        :description="enabled ? '暂无图谱数据，请先在「文档管理」入库或点击回填' : '文档知识图谱未启用'"
      >
        <el-button :icon="Delete" size="small" disabled>提示：需在服务端开启 DOC_KG_ENABLED</el-button>
      </el-empty>

      <div class="kg-legend">
        <div class="legend-group">
          <span class="legend-title">文档</span>
          <span class="legend-item"><i :style="{ background: DOC_COLORS.text }"></i>文本</span>
          <span class="legend-item"><i :style="{ background: DOC_COLORS.video_transcript }"></i>转录</span>
          <span class="legend-item"><i :style="{ background: DOC_COLORS.image }"></i>图片</span>
        </div>
        <div class="legend-group">
          <span class="legend-title">实体</span>
          <span class="legend-item"><i :style="{ background: ENTITY_COLORS.person }"></i>人物</span>
          <span class="legend-item"><i :style="{ background: ENTITY_COLORS.department }"></i>部门</span>
          <span class="legend-item"><i :style="{ background: ENTITY_COLORS.system }"></i>系统</span>
          <span class="legend-item"><i :style="{ background: ENTITY_COLORS.policy }"></i>制度</span>
          <span class="legend-item"><i :style="{ background: ENTITY_COLORS.term }"></i>术语</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.kg-page {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  gap: 12px;
  padding: 12px;
  overflow: hidden;
}

.kg-toolbar :deep(.el-card__body) {
  padding: 10px 16px;
}

.bar {
  display: flex;
  align-items: center;
  gap: 16px;
}

.title {
  font-size: 15px;
  font-weight: 600;
}

.hint {
  font-size: 12px;
  color: #909399;
}

.actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}

.kg-canvas-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  background: #fafcff;
  overflow: hidden;
}

.kg-canvas {
  width: 100%;
  height: 100%;
}

/* 空图提示浮在画布中央 */
.kg-empty {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
}

.kg-legend {
  position: absolute;
  left: 12px;
  bottom: 12px;
  display: flex;
  gap: 24px;
  padding: 8px 12px;
  background: rgba(255, 255, 255, 0.9);
  border: 1px solid #ebeef5;
  border-radius: 6px;
  font-size: 12px;
}

.legend-group {
  display: flex;
  align-items: center;
  gap: 10px;
}

.legend-title {
  font-weight: 600;
  color: #606266;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: #606266;
}

.legend-item i {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  display: inline-block;
}
</style>
