<script setup>
import { computed, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

// GFM 打开表格/删除线支持(AI 回答里的管道表格靠它渲染); breaks 让单换行也换行,
// 与纯文本时代的视觉习惯一致, 避免模型输出的软换行被并成一段。
marked.setOptions({ gfm: true, breaks: true })

const props = defineProps({
  content: { type: String, default: '' },
})

// 站内下载/产物路径(相对 `/api/files/...`)不带 scheme, marked 不会自动链接, 会被渲染成
// 不可点的裸文本(用户反馈"给了接口路径点不动")。这里兜底把正文里出现的 /api/... 包成
// 可点 <a>: 后端 FileResponse 带 Content-Disposition: attachment, 同源点击直接触发下载。
const API_PATH_RE = /\/api\/[A-Za-z0-9._/-]+/g

function autolinkApiPaths(root) {
  const skip = new Set(['A', 'CODE', 'PRE'])
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.includes('/api/')) return NodeFilter.FILTER_REJECT
      const p = node.parentElement
      if (p && (skip.has(p.tagName) || skip.has(p.parentElement?.tagName || ''))) return NodeFilter.FILTER_REJECT
      return NodeFilter.FILTER_ACCEPT
    },
  })
  const targets = []
  while (walker.nextNode()) targets.push(walker.currentNode)
  for (const textNode of targets) {
    const text = textNode.nodeValue
    const frag = document.createDocumentFragment()
    let last = 0
    API_PATH_RE.lastIndex = 0
    let m
    while ((m = API_PATH_RE.exec(text))) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)))
      const a = document.createElement('a')
      a.href = m[0]
      a.textContent = m[0]
      frag.appendChild(a)
      last = m.index + m[0].length
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)))
    textNode.parentNode.replaceChild(frag, textNode)
  }
}

// 净化后加工: 给代码块挂深色样式类并追加复制按钮; 并把裸 /api/ 下载路径自动链接化
// (v-html 场景下只能在这里动 DOM)
function postProcess(root) {
  for (const pre of Array.from(root.querySelectorAll('pre'))) {
    pre.classList.add('md-pre')
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'md-copy-btn'
    btn.textContent = '复制'
    pre.appendChild(btn)
  }
  autolinkApiPaths(root)
}

const html = computed(() => {
  const raw = DOMPurify.sanitize(marked.parse(props.content || '', { async: false }))
  const box = document.createElement('div')
  box.innerHTML = raw
  postProcess(box)
  return box.innerHTML
})

// 代码块复制: 事件委托挂在根节点上, 流式重渲染后依然生效
const copied = ref(false)
let copyTimer = null

async function onRootClick(e) {
  const btn = e.target.closest('.md-copy-btn')
  if (!btn) return
  const code = btn.closest('.md-pre')?.querySelector('code')?.innerText ?? btn.dataset.code ?? ''
  try {
    await navigator.clipboard.writeText(code)
    copied.value = true
    btn.textContent = '已复制'
  } catch {
    btn.textContent = '复制失败'
  }
  clearTimeout(copyTimer)
  copyTimer = setTimeout(() => {
    copied.value = false
    btn.textContent = '复制'
  }, 1600)
}
</script>

<template>
  <div class="md-body" @click="onRootClick" v-html="html" />
</template>

<style scoped>
.md-body {
  line-height: 1.7;
  word-break: break-word;
  font-size: 14px;
}

.md-body :first-child {
  margin-top: 0;
}

.md-body :last-child {
  margin-bottom: 0;
}

.md-body :deep(p) {
  margin: 0.4em 0;
}

.md-body :deep(ul),
.md-body :deep(ol) {
  margin: 0.4em 0;
  padding-left: 1.5em;
}

.md-body :deep(li) {
  margin: 0.15em 0;
}

.md-body :deep(h1),
.md-body :deep(h2),
.md-body :deep(h3),
.md-body :deep(h4),
.md-body :deep(h5),
.md-body :deep(h6) {
  margin: 0.8em 0 0.4em;
  font-weight: 600;
  line-height: 1.4;
}

.md-body :deep(h1) {
  font-size: 1.25em;
}

.md-body :deep(h2) {
  font-size: 1.15em;
}

.md-body :deep(h3) {
  font-size: 1.05em;
}

/* 表格: 有边框 + 表头底色, 宽度超出时横向滚动 */
.md-body :deep(table) {
  border-collapse: collapse;
  margin: 0.6em 0;
  display: block;
  max-width: 100%;
  overflow-x: auto;
}

.md-body :deep(th),
.md-body :deep(td) {
  border: 1px solid #dcdfe6;
  padding: 6px 12px;
  text-align: left;
}

.md-body :deep(th) {
  background: #f2f5fb;
  font-weight: 600;
  white-space: nowrap;
}

.md-body :deep(tr:nth-child(even) td) {
  background: #fafbfd;
}

.md-body :deep(blockquote) {
  margin: 0.5em 0;
  padding: 0.2em 0.8em;
  border-left: 3px solid #c0c4cc;
  color: #6b7280;
  background: #f7f8fb;
}

.md-body :deep(hr) {
  border: none;
  border-top: 1px solid #e3e6ee;
  margin: 0.8em 0;
}

.md-body :deep(a) {
  color: #409eff;
  text-decoration: none;
}

.md-body :deep(a:hover) {
  text-decoration: underline;
}

/* 行内代码 */
.md-body :deep(:not(pre) > code) {
  background: #eef1f6;
  color: #c7254e;
  border-radius: 4px;
  padding: 0.12em 0.4em;
  font-size: 0.92em;
  font-family: Consolas, Monaco, 'Courier New', monospace;
}

/* 代码块: 深色底 + 复制按钮 */
.md-body :deep(pre.md-pre) {
  position: relative;
  background: #282c34;
  color: #abb2bf;
  border-radius: 8px;
  padding: 10px 12px;
  margin: 0.6em 0;
  overflow-x: auto;
}

.md-body :deep(pre.md-pre code) {
  font-family: Consolas, Monaco, 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.6;
  background: none;
  color: inherit;
  padding: 0;
  white-space: pre;
}

.md-body :deep(.md-copy-btn) {
  position: absolute;
  top: 6px;
  right: 6px;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 5px;
  border: 1px solid #4b5263;
  background: #333842;
  color: #aab1c0;
  cursor: pointer;
  user-select: none;
}

.md-body :deep(.md-copy-btn:hover) {
  color: #fff;
  border-color: #6b7487;
}
</style>
