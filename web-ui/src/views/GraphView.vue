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
// 连线配色与线型：语义边(实体之间真实关系)实线加箭头, 结构边(文档提及)虚线无箭头。
// 以前只靠颜色深浅区分, 在一屏灰线里根本分不出来, 这就是"连线草率"的第一观感。
const EDGE_COLORS = { rel: '#8a94a6', mention: '#c9d6f2' }
const EDGE_LEGEND = [
  { key: 'rel', label: '语义关系', hint: '实线·箭头', dash: false },
  { key: 'mention', label: '文档提及', hint: '虚线', dash: true },
]
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
// 超过这个边数就默认不画关系名: 每个标签都是一个 shape, 几百个叠上去既糊又拖帧。
// 需要看的时候用工具栏开关手动打开。
const REL_LABEL_AUTO_MAX = 150

const loading = ref(false)
const building = ref(false)
const enabled = ref(true)
const truncated = ref(false)
const hiddenEdges = ref(0)
const showRelLabel = ref(true)
const stats = ref({ docs: 0, entities: 0, edges: 0 })
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

// 边 id: 同起点同终点同类型同关系只算一条(与 mergeGraph 的去重口径共用)。
// type 必须在 key 里: MENTIONS 的 label 是空串, 一旦关系词也为空就会与结构边撞 key,
// 后写入的那条会悄悄吃掉前一条。
function edgeId(e) {
  return `${e.source}->${e.target}#${e.type || ''}:${e.label || ''}`
}

// 样式一律在图配置里按数据算, 数据侧只带原始字段:
// 每次展开都重建上百份 style 对象是白花的开销, G6 自己会缓存计算结果
function toG6Node(n) {
  return { id: n.id, data: { ...n } }
}

function toG6Edge(e) {
  return {
    id: edgeId(e),
    source: e.source,
    target: e.target,
    // 关系名显不显示进数据而不只留在闭包里: G6 先拿新旧 datum 做差异比较, 判为没变就
    // 不会把元素排进更新队列(实测只改闭包里的 ref 时, updateEdgeData + draw 没有任何效果)。
    data: { ...e, labelVisible: showRelLabel.value && e.type === 'KG_REL' },
  }
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
  stats.value = { docs, entities, edges: edgeMap.size }
}

// 实体名在节点 id 里(形如 entity:名称::类型), 边的 tooltip 要还原成可读的两端名称
function nodeLabel(id) {
  if (!id) return ''
  return nodeMap.get(id)?.label || String(id).replace(/^entity:/, '').split('::')[0] || id
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

// 布局回调拿到的是 d3-force 内部的链接/节点对象: @antv/layout 会把原数据拷一份并另存到
// ``_original``, 字段到底挂在哪一层跟版本有关, 因此三种形状都兜一下,
// 不让布局因为取不到 data 而整体退化成同一个常数。
function edgeKind(link) {
  const type = link?.data?.type || link?.type || link?._original?.data?.type || ''
  return type === 'KG_REL' ? 'rel' : 'mention'
}

function nodeGroupOf(datum) {
  return datum?.data?.group || datum?.group || datum?._original?.data?.group || ''
}

function nodeDegreeOf(datum) {
  const raw = datum?.data?.degree ?? datum?.degree ?? datum?._original?.data?.degree
  return Number(raw) || 0
}

function tooltipContent(_event, items) {
  const d = items?.[0]?.data || {}
  let rows
  let title = d.label || d.doc_key || ''
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
  } else if (d.type === 'KG_REL') {
    // 语义边: 除了关系词还要说清"谁断言的", 否则图上有条线却不知它从哪里来
    title = `${nodeLabel(d.source)} → ${nodeLabel(d.target)}`
    rows = [
      ['关系', d.label || '相关'],
      ['来源文档', d.doc_count ? `${d.doc_count} 篇` : ''],
      ['出处', (d.doc_names || []).join('、')],
      ['依据', d.evidence || ''],
    ]
  } else {
    title = `${nodeLabel(d.source)} → ${nodeLabel(d.target)}`
    rows = [['关系', '文档提及']]
  }
  const box = document.createElement('div')
  box.className = 'kg-tip'
  const titleEl = document.createElement('div')
  titleEl.className = 'kg-tip-title'
  // 文档名/实体名都是外部数据, 只走 textContent, 不拼 innerHTML
  titleEl.textContent = title
  box.appendChild(titleEl)
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
        // 基准透明度/标签透明度必须显式给值: inactive 态退出时 G6 只把状态样式拿掉,
        // 基础样式里没有 opacity 可回退, 元素就会卡在淡出态(实测刷新才能恢复)。
        opacity: 1,
        labelOpacity: 1,
        labelText: (d) => d.data?.label || '',
        labelPlacement: 'bottom',
        labelFill: '#303133',
        labelFontSize: (d) => (isDoc(d) ? 12 : 10),
        labelFontWeight: (d) => (isDoc(d) ? 600 : 400),
      },
      state: {
        active: { lineWidth: 2.5, stroke: '#f7ba2a' },
        selected: { lineWidth: 3, stroke: '#ffcc00' },
        inactive: { opacity: 0.16, labelOpacity: 0 },
      },
    },
    edge: {
      // 二次曲线而不是直线: process-parallel-edges 的 bundle 模式靠给每条边分配
      // curveOffset 把平行边/反向边分开, 而 Line 元素不认 curveOffset(实测多条边仍完全
      // 重叠成同一条直线)。单独一条边时 transform 会把 curveOffset 置 0, 视觉上就是直线。
      type: 'quadratic',
      style: {
        stroke: (d) => (isRel(d) ? EDGE_COLORS.rel : EDGE_COLORS.mention),
        lineWidth: (d) => (isRel(d) ? 1.3 : 0.8),
        // 基准值给全: 与节点同理, inactive/active 态退出后要有可回退的原值
        opacity: 1,
        labelOpacity: 1,
        halo: false,
        // 结构边虚线、语义边实线([] 在 canvas/SVG 里就是无虚线样): 单靠颜色分不开两类边
        lineDash: (d) => (isRel(d) ? [] : [4, 3]),
        // MENTIONS 边不挂文字: 它是"文档提及实体"的结构含义, 写出来只是白画一遍文本
        labelText: (d) => (d.data?.labelVisible ? d.data?.label || '' : ''),
        // 不沿线旋转: 中文转到斜线上基本读不出, 而且带白底后水平摆放最好认
        labelAutoRotate: false,
        labelFontSize: 9,
        labelFill: '#606266',
        // 关系名必须有白底: 没背景时字直接压在灰线上, 线与字互相糊掉
        labelBackground: true,
        labelBackgroundFill: '#ffffff',
        labelBackgroundOpacity: 0.85,
        labelBackgroundRadius: 3,
        labelPadding: [1, 3],
        endArrow: (d) => isRel(d),
        endArrowSize: 6,
        endArrowType: 'triangle',
        endArrowFill: (d) => (isRel(d) ? EDGE_COLORS.rel : EDGE_COLORS.mention),
        haloStroke: '#f7ba2a',
        haloLineWidth: 5,
        haloOpacity: 0.25,
      },
      state: {
        active: { stroke: '#f7ba2a', lineWidth: 2, halo: true },
        // 悬停时把无关的边整片淡出, 才能"顺着线看清一个节点的关系"
        inactive: { opacity: 0.12, labelOpacity: 0 },
      },
    },
    layout: {
      type: 'd3-force',
      animation: false,
      iterations: LAYOUT_ITERATIONS,
      alphaDecay: LAYOUT_ALPHA_DECAY,
      preventOverlap: true,
      // 语义边拉远、结构边收紧: 文档与它提及的实体贴成一团, 实体之间的关系才有间距
      link: { distance: (e) => (edgeKind(e) === 'rel' ? 170 : 90) },
      // 碰撞半径要跟上节点实际直径(doc 直径 46, 实体按度数最大 40), 固定 30 会让
      // 节点贴脸重叠, 重叠部分的连线被节点盖住, 看上去就是"线断头/乱穿"
      collide: {
        radius: (d) => (nodeGroupOf(d) === 'doc' ? 34 : 26 + Math.min(10, nodeDegreeOf(d))),
        strength: 0.9,
        iterations: 2,
      },
      // 限制斥力作用距离: 少算远场作用力, 也避免整图被撑得过散导致缩到看不见标签
      manyBody: { distanceMax: 800 },
    },
    // 平行边处理: 同一对实体上的多条关系与 A↔B 反向边在直线下几何完全重合(看上去
    // 只有一条), 自环更是直接画不出来。bundle 模式会把参与分组的边改写成二次曲线并
    // 按组分配 curveOffset, 自环按 loopPlacement/loopDist 嵌套展开。
    transforms: [
      {
        type: 'process-parallel-edges',
        mode: 'bundle',
        distance: 12,
        loopMode: 'nested',
        loopDistance: 14,
      },
    ],
    behaviors: [
      'drag-canvas',
      'zoom-canvas',
      'drag-element',
      // degree=1: 默认 0 只点亮悬停的节点本身, 邻边一条都不亮, edge 的 active 态根本触发不到
      { type: 'hover-activate', degree: 1, state: 'active', inactiveState: 'inactive' },
      // 标签按重要度(度数)抽稀: 全画出来既糊成一片又拖慢每一帧, 放大后自动补回
      { type: 'auto-adapt-label', throttle: 150, padding: 2 },
    ],
    plugins: [{ type: 'tooltip', trigger: 'hover', getContent: tooltipContent }],
  })
  // 点击文档节点：拉取其邻域子图并增量合并进当前视图
  graph.on('node:click', (evt) => {
    const id = evt.target?.id
    const node = id ? nodeMap.get(id) : null
    if (node && node.group === 'doc') expandFocus(node.doc_key, node.label || node.doc_key)
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

// 关系名开关只重算样式: 数据与坐标都不动, 不该重跑力导向; 仍然排进同一条链,
// 避免连点开关与展开互踩。
// 重绘的触发方式很坑: 只把边浅拷贝一份回写是不行的(G6 比较 datum 后认为没变,
// 元素根本不入更新队列, 实测标签不会跟着开关变)。所以下面真的改一个数据字段
// ``data.labelVisible``, 让样式回调与数据差异指向同一个源。
function redraw() {
  renderChain = renderChain
    .catch(() => {})
    .then(async () => {
      if (!graph) return
      const visible = showRelLabel.value
      graph.updateEdgeData((prev) =>
        prev.map((edge) => ({
          ...edge,
          data: { ...edge.data, labelVisible: visible && edge.data?.type === 'KG_REL' },
        })),
      )
      await graph.draw()
    })
  return renderChain
}

async function expandFocus(docKey, title = docKey) {
  try {
    const data = await fetchGraph(docKey)
    const before = nodeMap.size
    mergeGraph(data)
    updateStats()
    await scheduleGraph('incremental')
    const added = nodeMap.size - before
    ElMessage.success(
      added > 0 ? `已展开「${title}」的关联邻域（+${added} 个节点）` : `「${title}」的关联邻域已在图中`,
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
    // 没有可访问溯源的历史边(迁移未跑)不会返回, 数量在这里告知用户该跑一次回填
    hiddenEdges.value = Number(data.hidden_edges) || 0
    mergeGraph(data)
    updateStats()
    // 小图默认开关系名, 大图默认关(每个边标签都是一个 shape, 几百个叠上去既糊又拖帧)
    showRelLabel.value = edgeMap.size <= REL_LABEL_AUTO_MAX
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
    // 全量回填现在只开给 admin(每点一次就是全表逐篇 LLM 抽取), 所以必须带上角色;
    // 已在跑时后端回 already_running, 不重复启动。
    const p = new URLSearchParams({ ...authParams(), role: current.role, operator: current.empId })
    const resp = await fetch(`/api/kg/rebuild-all?${p.toString()}`, {
      method: 'POST',
    })
    const data = await resp.json()
    if (!resp.ok) throw new Error(data.detail || resp.status)
    if (data.status === 'already_running') ElMessage.warning(data.message || '回填任务已在运行')
    else ElMessage.success(data.message || '回填已在后台启动')
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
          {{ stats.docs }} 篇文档 · {{ stats.entities }} 个实体 · {{ stats.edges }} 条连线 · 点击文档节点展开关联
        </span>
        <div class="actions">
          <el-tag v-if="truncated" size="small" type="warning" effect="plain">节点已达上限（已截断）</el-tag>
          <el-tag v-if="hiddenEdges" size="small" type="info" effect="plain">
            {{ hiddenEdges }} 条历史边无溯源·待回填
          </el-tag>
          <el-tag v-if="!enabled" size="small" type="danger" effect="plain">未启用</el-tag>
          <el-switch v-model="showRelLabel" size="small" active-text="关系名" @change="redraw" />
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
        <div class="legend-group">
          <span class="legend-title">连线</span>
          <span v-for="e in EDGE_LEGEND" :key="e.key" class="legend-item">
            <i class="line" :class="{ dashed: e.dash }" :style="{ borderColor: EDGE_COLORS[e.key] }"></i>
            {{ e.label }}（{{ e.hint }}）
          </span>
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

/* 开关被 flex 挤窄时文字会竖排与开关重叠, 固定不许折行/不许收缩 */
.actions :deep(.el-switch) {
  flex-shrink: 0;
}

.actions :deep(.el-switch__label) {
  white-space: nowrap;
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

/* 连线图例: 圆形色块当不了线型, 用一段带边框的短横线表示实线/虚线 */
.legend-item i.line {
  width: 18px;
  height: 0;
  border-radius: 0;
  border-top: 2px solid;
}

.legend-item i.line.dashed {
  border-top-style: dashed;
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
