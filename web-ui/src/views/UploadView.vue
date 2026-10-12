<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { UploadFilled, Refresh, Delete } from '@element-plus/icons-vue'
import { useEmployee } from '../composables/useEmployee'

const { current } = useEmployee()

const VIS_OPTIONS = [
  { value: 'public', label: '全员可见' },
  { value: 'dept', label: '指定部门可见' },
  { value: 'role', label: '指定角色可见' },
  { value: 'private', label: '仅自己可见' },
]

const ACCEPT = '.txt,.md,.pdf,.docx,.pptx,.xlsx,.srt,.vtt,.jpg,.jpeg,.png,.webp,.bmp'

// 后端按这份身份做文档 ACL 裁剪与"所有者/管理员"闸门(与 GraphView 同一口径):
// 缺了它们只能看到 public 文档, 也改不动任何文档的权限。
function authParams() {
  return { user_id: current.empId, operator: current.empId, role: current.role, department: current.department }
}

function authQuery() {
  return new URLSearchParams(authParams()).toString()
}

// 上传中的文件（解析后待确认入库）
const files = reactive([])
const dropHover = ref(false)
const fileInput = ref()

// 入库已改成异步任务（提交即返回 202 + task_id），所以进度靠轮询拿。
// 上限给到 15 分钟：扫描版 PDF 逐页 OCR 本来就可能是分钟级；超时只提示不报错，
// 因为任务还在后台跑，报「失败」会诱使用户重复提交。
const POLL_MS = 1500
const POLL_MAX_MS = 15 * 60 * 1000

const STAGE_TEXT = {
  pending: '排队中',
  parsing: '解析中（OCR/切块）…',
  embedding: '向量化中…',
  publishing: '发布知识块中…',
  indexing: '建索引/图谱中…',
  ready: '入库成功',
  failed: '入库失败',
}
const UPLOAD_STATUS_TEXT = {
  pending: '排队解析中…',
  parsing: '解析中（OCR/切块）…',
  parsed: '解析完成，请选择标签',
  failed: '解析失败',
  expired: '上传已过期，请重新上传',
}
const DOC_STATUS_TEXT = { ready: '已就绪', ingesting: '入库中', failed: '入库失败', pending: '待入库' }

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function pickFiles() {
  fileInput.value?.click()
}
function onFileChange(e) {
  // FileList 是 input 的活引用，必须先快照成数组，否则下面清空 input 会把待处理文件一起清空
  const list = Array.from(e.target.files || [])
  e.target.value = ''
  handleFiles(list)
}
function onDrop(e) {
  dropHover.value = false
  handleFiles(Array.from(e.dataTransfer?.files || []))
}

async function handleFiles(fileList) {
  for (const file of Array.from(fileList || [])) {
    const item = reactive({
      key: file.name + '-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6),
      filename: file.name,
      status: '上传中…',
      state: 'uploading', // uploading | parsing | parsed | ingesting | done | error
      uploadId: '',
      taskId: '',
      modality: '',
      overwritten: false,
      preview: '',
      previewLen: 0,
      tags: [],
      customTag: '',
      visibility: 'public',
      deptId: '',
      allowedRoles: '',
    })
    files.push(item)
    try {
      const fd = new FormData()
      fd.append('file', file)
      fd.append('uploader', current.empId)
      const resp = await fetch('/api/docs/upload', { method: 'POST', body: fd })
      const data = await resp.json()
      if (!resp.ok) throw new Error(data.detail || resp.status)
      // 202：此时只有 upload_id，预览与建议标签要等 worker 把解析产物写出来
      item.uploadId = data.upload_id
      item.overwritten = !!data.overwritten
      item.state = 'parsing'
      item.status = UPLOAD_STATUS_TEXT.pending
      pollUpload(item)
    } catch (e) {
      item.state = 'error'
      item.status = '失败: ' + e.message
    }
  }
}

async function pollUpload(item) {
  const started = Date.now()
  while (Date.now() - started < POLL_MAX_MS) {
    await sleep(POLL_MS)
    let data
    try {
      const resp = await fetch(
        `/api/docs/uploads/${encodeURIComponent(item.uploadId)}?${authQuery()}`
      )
      data = await resp.json()
      if (!resp.ok) throw new Error(data.detail || resp.status)
    } catch (e) {
      // 单次轮询失败不判死：网络抖动很常见，下一轮接着问
      item.status = '解析状态查询失败（重试中）: ' + e.message
      continue
    }
    item.modality = data.modality || item.modality
    item.overwritten = !!data.overwritten
    if (data.status === 'parsed') {
      item.preview = data.preview || ''
      item.previewLen = data.preview_len || 0
      item.tags = [...(data.suggested_tags || [])]
      item.state = 'parsed'
      const warn = (data.warnings || []).length ? `（${data.warnings.length} 条告警）` : ''
      item.status =
        (data.overwritten ? '已存在同名文档，确认后将覆盖更新' : UPLOAD_STATUS_TEXT.parsed) + warn
      return
    }
    if (data.status === 'failed' || data.status === 'expired') {
      item.state = 'error'
      const base = UPLOAD_STATUS_TEXT[data.status] || '解析未成功'
      item.status = data.error ? `${base}: ${data.error}` : base
      return
    }
    item.status = UPLOAD_STATUS_TEXT[data.status] || data.status
  }
  item.status = '仍在后台解析中（扫描 PDF 可能需数分钟），可稍后在列表查看'
}

function addCustomTag(item) {
  const v = item.customTag.trim()
  if (v && !item.tags.includes(v)) item.tags.push(v)
  item.customTag = ''
}
function removeTag(item, tag) {
  item.tags = item.tags.filter((t) => t !== tag)
}

async function confirmIngest(item) {
  item.state = 'ingesting'
  item.status = STAGE_TEXT.pending
  try {
    const resp = await fetch('/api/docs/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upload_id: item.uploadId,
        tags: item.tags,
        uploader: current.empId,
        visibility: item.visibility,
        dept_id: item.deptId.trim(),
        allowed_roles: splitRoles(item.allowedRoles),
        role: current.role,
        department: current.department,
      }),
    })
    const res = await resp.json()
    if (!resp.ok) throw new Error(res.detail || resp.status)
    // 202 + task_id：入库进度由任务表给，不再靠这一个请求的返回体
    item.taskId = res.task_id
    pollTask(item)
    loadDocs()
  } catch (e) {
    item.state = 'parsed'
    item.status = '入库提交失败: ' + e.message
    ElMessage.error('入库提交失败: ' + e.message)
  }
}

async function pollTask(item) {
  const started = Date.now()
  while (Date.now() - started < POLL_MAX_MS) {
    await sleep(POLL_MS)
    let data
    try {
      const resp = await fetch(`/api/docs/tasks/${encodeURIComponent(item.taskId)}?${authQuery()}`)
      data = await resp.json()
      if (!resp.ok) throw new Error(data.detail || resp.status)
    } catch (e) {
      item.status = '入库状态查询失败（重试中）: ' + e.message
      continue
    }
    if (data.stage === 'ready') {
      item.state = 'done'
      const warn = (data.warnings || []).length ? `，告警[${data.warnings.join('; ')}]` : ''
      item.status = `入库成功：${data.chunk_count} 个知识块，标签[${item.tags.join(', ')}]${warn}`
      loadDocs()
      return
    }
    if (data.stage === 'failed') {
      item.state = 'parsed'
      item.status = `入库失败（第 ${data.attempt} 次）: ${data.error || '未知原因'}`
      ElMessage.error('入库失败: ' + (data.error || '未知原因'))
      loadDocs()
      return
    }
    item.status = `${STAGE_TEXT[data.stage] || data.stage}（第 ${data.attempt} 次尝试）`
  }
  item.status = '仍在后台入库中，可稍后在列表查看状态'
  loadDocs()
}

function splitRoles(s) {
  return (s || '')
    .split(/[,，]/)
    .map((x) => x.trim())
    .filter(Boolean)
}

// --- 已入库文档列表 ---
const docs = ref([])
const loadingDocs = ref(false)

// 上传文档弹窗
const uploadDialogVisible = ref(false)

async function loadDocs() {
  loadingDocs.value = true
  try {
    const resp = await fetch(`/api/docs?${authQuery()}`)
    docs.value = await resp.json()
  } catch (e) {
    docs.value = []
    ElMessage.error('文档列表加载失败: ' + e.message)
  } finally {
    loadingDocs.value = false
  }
  // 入库是异步的: 列表里有「入库中」的文档时自动继续刷新，
  // 否则用户会看到一个不动的中间态，只能靠手动反复点刷新。
  if (docs.value.some((d) => d.status && d.status !== 'ready')) {
    setTimeout(loadDocs, 4000)
  }
}

async function changeAcl(row) {
  const visibility = row.visibility
  let deptId = row.dept_id || ''
  let roles = row.allowed_roles || []
  try {
    if (visibility === 'dept') {
      const { value } = await ElMessageBox.prompt('授权部门（如 研发部）', '设置权限', {
        inputValue: row.dept_id || '',
        inputValidator: (v) => (v && v.trim() ? true : '部门可见必须填写部门'),
      })
      deptId = value.trim()
    } else if (visibility === 'role') {
      const { value } = await ElMessageBox.prompt('授权角色（逗号分隔，如 hr,finance）', '设置权限', {
        inputValue: (row.allowed_roles || []).join(','),
        inputValidator: (v) => (v && v.trim() ? true : '角色可见必须填写角色'),
      })
      roles = splitRoles(value)
    }
  } catch {
    loadDocs() // 取消则回滚显示
    return
  }
  try {
    const resp = await fetch(`/api/docs/${encodeURIComponent(row.doc_key)}/acl`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        visibility,
        dept_id: deptId,
        allowed_roles: roles,
        ...authParams(),
      }),
    })
    const data = await resp.json()
    if (!resp.ok) throw new Error(data.detail || resp.status)
    if (data.warning) ElMessage.warning('⚠ ' + data.warning)
    else ElMessage.success('权限已更新')
    loadDocs()
  } catch (e) {
    ElMessage.error('权限修改失败: ' + e.message)
    loadDocs()
  }
}

async function deleteDoc(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除文档「${row.name}${row.ext}」吗？将同时移除向量库中的知识块，不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    const resp = await fetch(
      `/api/docs/${encodeURIComponent(row.doc_key)}?${authQuery()}`,
      { method: 'DELETE' }
    )
    const data = await resp.json()
    if (!resp.ok) throw new Error(data.detail || resp.status)
    ElMessage.success('已删除')
    loadDocs()
  } catch (e) {
    ElMessage.error('删除失败: ' + e.message)
  }
}

function visTagType(v) {
  return { public: 'success', dept: 'primary', role: 'warning', private: 'info' }[v] || 'info'
}
function visLabel(v) {
  return { public: '全员', dept: '部门', role: '角色', private: '仅自己' }[v] || v
}
// 旧文档没有 status 列值时按「已就绪」看（后端默认值也走这个口径）。
function statusLabel(v) {
  return DOC_STATUS_TEXT[v || 'ready'] || v
}
function statusTagType(v) {
  return { ready: 'success', ingesting: 'warning', failed: 'danger', pending: 'info' }[v || 'ready'] || 'info'
}

onMounted(loadDocs)
</script>

<template>
  <div class="upload-page">
    <div class="col">
      <el-card shadow="never" class="block">
        <template #header>
          <div class="list-header">
            <span class="card-title">已入库文档</span>
            <div class="list-header-actions">
              <el-button :icon="UploadFilled" size="small" type="primary" plain
                @click="uploadDialogVisible = true">上传</el-button>
              <el-button :icon="Refresh" size="small" :loading="loadingDocs" @click="loadDocs">刷新</el-button>
            </div>
          </div>
        </template>
        <el-table :data="docs" v-loading="loadingDocs" size="small" empty-text="暂无文档">
          <el-table-column label="名称" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.name }}{{ row.ext }}</template>
          </el-table-column>
          <el-table-column prop="modality" label="模态" width="80" />
          <el-table-column label="状态" width="84">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="chunk_count" label="块数" width="64" />
          <el-table-column label="标签" min-width="140">
            <template #default="{ row }">
              <el-tag v-for="t in row.tags || []" :key="t" size="small" class="tag-pill">{{ t }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="权限" min-width="150">
            <template #default="{ row }">
              <el-select :model-value="row.visibility" size="small" style="width: 92px"
                @change="(v) => { row.visibility = v; changeAcl(row) }">
                <el-option label="全员" value="public" />
                <el-option label="部门" value="dept" />
                <el-option label="角色" value="role" />
                <el-option label="仅自己" value="private" />
              </el-select>
              <el-tag v-if="row.visibility === 'dept' && row.dept_id" size="small" type="primary" class="vis-pill">
                {{ row.dept_id }}
              </el-tag>
              <el-tag v-if="row.visibility === 'role' && (row.allowed_roles || []).length" size="small" type="warning"
                class="vis-pill">
                {{ row.allowed_roles.join(',') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_by" label="上传人" width="90" />
          <el-table-column prop="updated_at" label="更新时间" width="150" show-overflow-tooltip />
          <el-table-column label="操作" width="72" fixed="right">
            <template #default="{ row }">
              <el-button :icon="Delete" size="small" type="danger" link @click="deleteDoc(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <el-dialog v-model="uploadDialogVisible" title="上传文档" width="560px" append-to-body>
      <div class="dropzone" :class="{ hover: dropHover }" @click="pickFiles" @dragover.prevent="dropHover = true"
        @dragleave.prevent="dropHover = false" @drop.prevent="onDrop">
        <el-icon class="up-icon">
          <UploadFilled />
        </el-icon>
        <p>点击选择或拖拽文件到此处（可多选）</p>
        <p class="accept">支持 txt / md / pdf / docx / pptx / xlsx / srt / vtt / 图片，单文件 ≤50MB</p>
        <input ref="fileInput" type="file" multiple hidden :accept="ACCEPT" @change="onFileChange" />
      </div>

      <div v-for="item in files" :key="item.key" class="file-item">
        <div class="file-head">
          <span class="file-name">{{ item.filename }}</span>
          <div class="file-head-right">
            <el-tag v-if="item.uploadId" size="small" :type="item.overwritten ? 'warning' : 'success'">
              {{ item.overwritten ? '覆盖更新' : '新文档' }}
            </el-tag>
            <el-tag v-if="item.modality" size="small" type="info">{{ item.modality }}</el-tag>
          </div>
        </div>
        <div class="status" :class="{ err: item.state === 'error' }">{{ item.status }}</div>

        <el-collapse v-if="item.preview" class="preview">
          <el-collapse-item title="内容预览" name="p">
            <pre>{{ item.preview }}{{ item.previewLen > 500 ? ' …' : '' }}</pre>
          </el-collapse-item>
        </el-collapse>

        <template v-if="item.state === 'parsed' || item.state === 'ingesting'">
          <div class="row">
            <span class="row-label">分类标签：</span>
            <el-tag v-for="t in item.tags" :key="t" closable :disable-transitions="false" @close="removeTag(item, t)">
              {{ t }}
            </el-tag>
            <el-input v-model="item.customTag" size="small" placeholder="自定义标签，回车添加" style="width: 180px"
              @keyup.enter="addCustomTag(item)" />
          </div>
          <div class="row">
            <span class="row-label">文档权限：</span>
            <el-select v-model="item.visibility" size="small" style="width: 140px">
              <el-option v-for="o in VIS_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
            <el-input v-if="item.visibility === 'dept'" v-model="item.deptId" size="small" placeholder="部门，如 研发部"
              style="width: 160px" />
            <el-input v-if="item.visibility === 'role'" v-model="item.allowedRoles" size="small"
              placeholder="角色，逗号分隔 如 hr,finance" style="width: 220px" />
            <el-button type="primary" size="small" :loading="item.state === 'ingesting'" @click="confirmIngest(item)">
              确认入库
            </el-button>
          </div>
        </template>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.upload-page {
  flex: 1;
  min-height: 0;
  overflow: auto;
  display: flex;
  gap: 18px;
  padding: 18px 20px;
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
  align-items: flex-start;
}

.col {
  flex: 1;
  min-width: 0;
}

.block {
  border-radius: 12px;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #1f3a93;
}

.list-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.list-header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dropzone {
  border: 2px dashed #b9c4dd;
  border-radius: 10px;
  padding: 30px;
  text-align: center;
  color: #67718a;
  cursor: pointer;
  transition: 0.2s;
}

.dropzone.hover {
  border-color: #1f6feb;
  background: #f0f5ff;
}

.dropzone p {
  font-size: 13px;
  line-height: 1.8;
}

.up-icon {
  font-size: 42px;
  color: #b9c4dd;
  margin-bottom: 6px;
}

.accept {
  font-size: 11px;
  color: #9aa3b8;
}

.file-item {
  border: 1px solid #e3e6ee;
  border-radius: 10px;
  padding: 12px 14px;
  margin-top: 12px;
}

.file-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.file-head-right {
  display: flex;
  gap: 6px;
}

.file-name {
  font-size: 14px;
  font-weight: 600;
}

.status {
  font-size: 12px;
  color: #67718a;
  margin-top: 4px;
}

.status.err {
  color: #cf222e;
}

.preview {
  margin-top: 8px;
}

.preview pre {
  font-size: 12px;
  color: #67718a;
  background: #f6f8fa;
  border-radius: 6px;
  padding: 8px 10px;
  max-height: 120px;
  overflow: auto;
  white-space: pre-wrap;
}

.row {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.row-label {
  font-size: 12px;
  color: #67718a;
}

.tag-pill {
  margin: 1px 2px;
}

.vis-pill {
  margin-left: 4px;
}
</style>
