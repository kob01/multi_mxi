<script setup>
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Plus, Delete, FullScreen } from '@element-plus/icons-vue'
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
// tooltip 用的中文口径, 与图例保持一致
const MODALITY_LABELS = { text: '文本', video_transcript: '转录', image: '图片' }
const TYPE_LABELS = {
  person: '人物',
  department: '部门',
  system: '系统',
  document: '文档',
  policy: '制度',
  term: '术语',
  other: '其他',
}

// 布局迭代次数与衰减率配套(alphaDecay 0.028 约 250 次迭代收敛到 alphaMin), 一次算完终态
const LAYOUT_ITERATIONS = 250
const LAYOUT_ALPHA_DECAY = 0.028

const loading = ref(false)
const building = ref(false)
const enabled = ref(true)
const truncated = ref(false)
const stats = ref({ docs: 0, entities: 0 })
const graphEl = ref()

let graph = null
// G6 的 render/layout/draw 都是异步的, 用一条 promise 链串行排队, 连点展开时不会并发互踩
let renderChain = Promise.resolve()
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

// 边 id: 同起点同终点同关系只算一条(与 mergeGraph 的去重口径共用)
function edgeId(e) {
  return `${e.source}->${e.target}#${e.label || ''}`
}

// 样式一律在图配置里按数据算, 数据侧只带原始字段:
// 每次展开都重建上百份 style 对象是白花的开销, G6 自己会缓存计算结果
function toG6Node(n) {
  return { id: n.id, data: { ...n } }
}

function toG6Edge(e) {
  return { id: edgeId(e), source: e.source, target: e.target, data: { ...e } }
}

function mergeGraph(data) {
  for (const n of data.nodes || []) {
    if (!nodeMap.has(n.id)) nodeMap.set(n.id, n)
  }
  for (const e of data.edges || []) {
    const key = edgeId(e)
    if (!edgeMap.has(key)) edgeMap.set(key, e)
  }
}

function updateStats() {
  let docs = 0
  let entities = 0
  for (const n of nodeMap.values()) {
    if (n.group === 'doc') docs += 1
    else entities += 1
  }
  stats.value = { docs, entities }
}

function isDoc(datum) {
  return datum?.data?.group === 'doc'
}

function isRel(datum) {
  return datum?.data?.type === 'KG_REL'
}

// 实体按关联文档数放大: 枢纽实体更显眼, 也让标签抽稀优先保住它们
function entitySize(datum) {
  const degree = Number(datum?.data?.degree) || 0
  return Math.min(20 + degree * 2, 40)
}

function tooltipContent(_event, items) {
  const d = items?.[0]?.data || {}
  let rows
  if (d.group === 'doc') {
    rows = [
      ['类别', `文档 · ${MODALITY_LABELS[d.modality] || d.modality || ''}`],
      ['标识', d.doc_key || ''],
      ['提示', '点击展开关联邻域'],
    ]
  } else if (d.group === 'entity') {
    rows = [
      ['类别', `实体 · ${TYPE_LABELS[d.type] || d.type || ''}`],
      ['关联文档', `${d.degree || 0} 篇`],
    ]
  } else {
    rows = [['关系', d.label || (d.type === 'MENTIONS' ? '提及' : '')]]
  }
  const box = document.createElement('div')
  box.className = 'kg-tip'
  const title = document.createElement('div')
  title.className = 'kg-tip-title'
  // 文档名/实体名都是外部数据, 只走 textContent, 不拼 innerHTML
  title.textContent = d.label || d.doc_key || ''
  box.appendChild(title)
  for (const [k, v] of rows) {
    if (!v) continue
    const row = document.createElement('div')
    row.className = 'kg-tip-row'
    const key = document.createElement('span')
    key.textContent = k
    const val = document.createElement('span')
    val.textContent = String(v)
    row.append(key, val)
    box.appendChild(row)
  }
  return box
}

function ensureGraph() {
  if (graph || !graphEl.value) return
  graph = new Graph({
    container: graphEl.value,
    autoResize: true,
    autoFit: 'view',
    padding: 32,
    // 全局关动画是本次提速的主刀: 力导向默认按 tick 逐帧重绘(iterations 次),
    // 上百节点 + 数百条边 + 同量级文本时每帧都要全量重画, 这就是"卡"的来源。
    // 关掉后布局一次算完终态只绘制一帧, 元素入场与视口过渡也一并取消。
    animation: false,
    data: { nodes: [], edges: [] },
    node: {
      type: 'circle',
      style: {
        fill: (d) =>
          isDoc(d)
            ? DOC_COLORS[d.data.modality] || DOC_COLORS.text
            : ENTITY_COLORS[d.data.type] || ENTITY_COLORS.other,
        size: (d) => (isDoc(d) ? 46 : entitySize(d)),
        stroke: '#fff',
        lineWidth: 1.5,
        labelText: (d) => d.data?.label || '',
        labelPlacement: 'bottom',
        labelFill: '#303133',
        labelFontSize: (d) => (isDoc(d) ? 12 : 10),
        labelFontWeight: (d) => (isDoc(d) ? 600 : 400),
      },
      state: {
        active: { lineWidth: 2.5, stroke: '#f7ba2a' },
        selected: { lineWidth: 3, stroke: '#ffcc00' },
      },
    },
    edge: {
      type: 'line',
      style: {
        stroke: (d) => (isRel(d) ? '#909399' : '#c0cdf0'),
        lineWidth: (d) => (isRel(d) ? 1.4 : 1),
        // MENTIONS 边不挂文字: 它是"文档提及实体"的结构含义, 写出来只是白画一遍文本
        labelText: (d) => (isRel(d) ? d.data?.label || '' : ''),
        labelFontSize: 9,
        labelFill: '#909399',
        endArrow: (d) => isRel(d),
      },
      state: { active: { stroke: '#f7ba2a', lineWidth: 2 } },
    },
    layout: {
      type: 'd3-force',
      animation: false,
      iterations: LAYOUT_ITERATIONS,
      alphaDecay: LAYOUT_ALPHA_DECAY,
      preventOverlap: true,
      link: { distance: 130 },
      collide: { radius: 30, strength: 0.8 },
      // 限制斥力作用距离: 少算远场作用力, 也避免整图被撑得过散导致缩到看不见标签
      manyBody: { distanceMax: 800 },
    },
    behaviors: [
      'drag-canvas',
      'zoom-canvas',
      'drag-element',
      'hover-activate',
      // 标签按重要度(度数)抽稀: 全画出来既糊成一片又拖慢每一帧, 放大后自动补回
      { type: 'auto-adapt-label', throttle: 150, padding: 2 },
    ],
    plugins: [{ type: 'tooltip', trigger: 'hover', getContent: tooltipContent }],
  })
  // 点击文档节点：拉取其邻域子图并增量合并进当前视图
  graph.on('node:click', (evt) => {
    const id = evt.target?.id
    const node = id ? nodeMap.get(id) : null
    if (node && node.group === 'doc') expandFocus(node.doc_key)
  })
}

async function applyGraph(mode) {
  if (!graph) return
  const nodes = [...nodeMap.values()].map(toG6Node)
  const edges = [...edgeMap.values()].map(toG6Edge)
  if (mode === 'incremental') {
    // 只喂新增元素: 老节点保留当前坐标当力导向种子, 整图不从头重排, 视口也不动
    const knownNodes = new Set(graph.getNodeData().map((n) => n.id))
    const knownEdges = new Set(graph.getEdgeData().map((e) => e.id))
    const freshNodes = nodes.filter((n) => !knownNodes.has(n.id))
    const freshEdges = edges.filter((e) => !knownEdges.has(e.id))
    if (!freshNodes.length && !freshEdges.length) return
    graph.addData({ nodes: freshNodes, edges: freshEdges })
    await graph.layout()
    await graph.draw()
    return
  }
  graph.setData({ nodes, edges })
  await graph.render()
}

function scheduleGraph(mode) {
  // 上一次失败不能让链子断掉, 否则后续渲染全部静默不执行
  renderChain = renderChain.catch(() => {}).then(() => applyGraph(mode))
  return renderChain
}

async function expandFocus(docKey) {
  try {
    const data = await fetchGraph(docKey)
    const before = nodeMap.size
    mergeGraph(data)
    updateStats()
    await scheduleGraph('incremental')
    const added = nodeMap.size - before
    ElMessage.success(
      added > 0 ? `已展开「${docKey}」的关联邻域（+${added} 个节点）` : `「${docKey}」的关联邻域已在图中`,
    )
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
    updateStats()
    ensureGraph()
    await scheduleGraph('full')
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

// 展开邻域不再自动重适配视口(否则看局部时镜头一直被拉走), 需要回全图时手动一键
function fitView() {
  graph?.fitView()
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
          <el-button :icon="FullScreen" size="small" text @click="fitView">适应画布</el-button>
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

<style>
/* tooltip 内容由 JS 动态建在画布容器里, scoped 选择器抓不到, 故单独一块全局样式(类名已前缀隔离) */
.kg-tip {
  font-size: 12px;
  line-height: 1.6;
  color: #303133;
}

.kg-tip-title {
  font-weight: 600;
  margin-bottom: 2px;
  max-width: 260px;
  word-break: break-all;
}

.kg-tip-row {
  display: flex;
  gap: 8px;
  color: #606266;
}

.kg-tip-row span:first-child {
  color: #909399;
  flex-shrink: 0;
}
</style>
