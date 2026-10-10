"""生成交互式 HTML 架构图 (markmap，浏览器直接打开)"""
html = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>马小i 多智能体 AI 系统 — 架构图</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: "Microsoft YaHei", sans-serif; }
  #markmap { width: 100vw; height: 100vh; }
  .toolbar {
    position: fixed; top: 12px; right: 16px; z-index: 10;
    display: flex; gap: 8px;
  }
  .toolbar button {
    padding: 6px 14px; border: 1px solid #ccc; border-radius: 6px;
    background: #fff; cursor: pointer; font-size: 13px;
  }
  .toolbar button:hover { background: #f0f0f0; }
</style>
</head>
<body>
<div class="toolbar">
  <button onclick="mm.fit()">适应画布</button>
  <button onclick="mm.foldAll(-1, true)">全展开</button>
  <button onclick="mm.foldAll(1, true)">收起至 L1</button>
</div>
<svg id="markmap"></svg>

<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script src="https://cdn.jsdelivr.net/npm/markmap-view@0.15"></script>
<script src="https://cdn.jsdelivr.net/npm/markmap-lib@0.15/dist/browser/index.js"></script>

<script>
const md = `
# 马小i 多智能体 AI 系统

## ① 入口层 Gateway
- FastAPI 网关 (app/main.py)
- Vue3 SPA 前端 (web-ui/)
- REST 路由注册
  - /api/chat · /api/agents → assistant 流式对话
  - /api/docs · /api/files → 文档/文件管理
  - /api/memory · /api/kg → 记忆/知识图谱
  - /api/dataops → 数据运维

## ② 调度核心 Assistant (LangGraph)
- 编排节点流程
  - build_context → resolve_time
  - rewrite_query → classify_intent
  - 四路分发: kb_retrieve / kb_requery / kb_generate
  - tool_execute / agent_delegate / chitchat
  - multi_agent_execute (并发委派，跳过意图漏斗)
  - persist_memory (写回记忆)
- 意图识别三层漏斗
  - L1 规则快筛: 正则/关键词, 0 ms
  - L2 bge-m3 语义分类: Top-K 均值 ≥ 0.62, margin ≥ 0.05
  - L3 deepseek LLM 兜底
  - 终端关键词安全网 fallback
- 多智能体并发委派
  - agent_targets 显式点选触发
  - Semaphore 并发控制 (默认并行度=2)
  - 逐位超时: 外层 150s / 内层 send_guarded 120s
  - 合并: 一节一智能体 Markdown
  - 开关: MULTI_AGENT_ENABLED=false 回退单意图链路

## ③ RAG 知识底座
- 混合检索
  - 稠密向量: bge-m3 + pgvector HNSW (cosine)
  - 稀疏倒排: BM25 (Elasticsearch + jieba)
  - RRF 融合排序
- 重排序 Re-rank
  - bge-reranker-v2-m3 (真 cross-encoder)
  - 部署: compose 内 TEI 容器
- 文档入库流水线
  - upload → parse(MinerU OCR) → chunk(结构化切块)
  - → embed(bge-m3) → pgvector upsert → ES 增量
  - → 缓存失效通知
- 文档级权限 RAG ACL
  - 前置裁剪: 按 user_id/tags 过滤 doc_key
  - 最终复核: 生成前二次核验

## ④ MCP 业务封装层 (FastMCP)
- hr-mcp (:18001) — 人事工具集
- finance-mcp (:18002) — 财务工具集
- analytics-mcp — 数据洞察工具集
- procurement-mcp — 采购工具集
- Text2SQL: PG 方言 + sql_guard 黑名单
- 同步 DB 访问: psycopg3 (app/db/sync.py)

## ⑤ A2A 专业智能体
- HR Agent — 人事专业
- Finance Agent — 财务专业
- Analyst Agent — 数据洞察专业
- Contract Agent — 合同专业
- 协议: JSON-RPC over HTTP (a2a-sdk)
- 能力声明: agent_card.py
- 注册表: AGENT_URLS / AGENT_PROFILES (a2a_client.py)

## ⑥ 记忆层
- 会话记忆 Session Memory
  - Redis 滚动窗口 + 摘要压缩
  - 降级: 进程内 dict
- 工作状态 Checkpointer
  - AsyncRedisSaver (langgraph-checkpoint-redis)
  - 降级: InMemorySaver
- 个人级六桶记忆 Personal Memory
  - User Memory → user_profiles 表 (画像/偏好/习惯)
  - Episodic Memory → occurred_at 时间锚点
  - Personal Knowledge → kind=knowledge
  - Personal Graph → Neo4j 个人图谱
  - 桶语义单一事实源: taxonomy.py
  - 召回: build_context 一次 embedding 并行六桶
  - 写入: persist_memory 一次 LLM 调用分桶落盘
- 长期记忆双通道
  - Vector 通道: pgvector
  - Graph 通道: Neo4j

## ⑦ 缓存层 Cache (Redis)
- Prompt Cache — LLM 系统提示词
- Retrieval Cache — RAG 检索结果
- Tool Cache — MCP 工具结果 (仅只读，A2A 委派不缓存)
- 全路径可降级，Redis 不可用时直接穿透

## ⑧ 安全治理体系
- 身份认证 (auth.py) — Bearer Token + 角色白名单
- 调用者上下文 (caller.py) — scope_id 多租户隔离
- 文档级 ACL (acl.py)
- 信息脱敏 (masking.py)
- 全链路审计 (audit.py) — trace_id 串联, audit.jsonl
- 配额控制 (quota.py)
- URL/SSRF 防护 (url_guard.py)
- SQL Guard — 黑名单 + SET LOCAL statement_timeout
- AST Guard — Text2SQL AST 级白名单校验

## ⑨ 文档知识图谱 GraphRAG
- Neo4j 存储 (:KgDoc / :KgEntity / [:KG_REL])
- LLM 关系抽取 (app/kg/extract.py)
- 受控关系词表 (vocab.py)
- 边级 ACL — KG_REL.docs 溯源字段
- 前端: AntV G6 v5 力导向图 (GraphView.vue)

## ⑩ 存储层 (三层分工)
- PostgreSQL + pgvector [统一持久化]
  - documents / tags — 文档元数据
  - knowledge_chunks — 知识块向量 (HNSW)
  - long_term_memories — 长期/个人记忆
  - user_profiles — 用户画像 JSONB
  - hr_* / finance_* — 业务数据表
- MongoDB [正文外置]
  - raw_text / normalized_text / structure
  - 父块 context (按 parent_id 批量取)
- Elasticsearch [BM25 派生倒排]
  - content_tokens 中文分词
  - 可从 PG 全量 rebuild
- Redis Stack [缓存+会话+Checkpointer]
- Neo4j [文档图谱+个人图谱]

## ⑪ 工具与能力
- 联网搜索 search_web
  - deepseek Responses API (url_citation 引用)
  - DDGS / Serper / Tavily
  - 开关: WEB_SEARCH_PROVIDER
- 文档生成 docgen
  - DOCX / PDF / PPTX / XLSX / Markdown
- 数据分析 analytics
- 文件管理 files

## ⑫ 可观测性 Observability
- LangSmith Tracing (云端)
- Langfuse 自建可观测栈 (compose 内)
- trace_id 全链路串联
- audit.jsonl 结构化审计日志
- 能力开关摘要日志 (启动时 on/off 清单)

## ⑬ 基础设施 Docker Compose
- assistant :18000 — FastAPI 网关
- postgres :5432 — pgvector/pg16
- redis :6379 — Redis Stack
- elasticsearch :9200
- neo4j :17474/17687 — HTTP/Bolt
- mongodb :27017
- tei-reranker :8080
- mineru :8002 — OCR
- hr-mcp / finance-mcp / analytics-mcp / procurement-mcp
- hr-agent / finance-agent / analyst-agent / contract-agent
- langfuse (可选) — 自建可观测
- 密钥: docker/secrets/ 文件挂载, 不进 git/env
`;

const { Markmap } = window.markmap;
const { Transformer } = window.markmap;
const transformer = new Transformer();
const { root } = transformer.transform(md);
const mm = Markmap.create('#markmap', {
  colorFreezeLevel: 2,
  duration: 400,
  maxWidth: 320,
}, root);
</script>
</body>
</html>
"""

out = r"d:\ai\mxi\docs-interview\马小i系统架构图.html"
with open(out, "w", encoding="utf-8") as f:
    f.write(html.lstrip())
print("生成完成:", out)
