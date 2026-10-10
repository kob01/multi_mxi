"""生成系统架构 FreeMind (.mm) 思维导图文件"""
import xml.etree.ElementTree as ET
from xml.dom import minidom

# ---------- 构建节点 ----------
def add_node(parent: ET.Element, text: str, children=None, color=None, folded=False):
    node = ET.SubElement(parent, "node", TEXT=text)
    if color:
        node.set("COLOR", color)
    if folded:
        node.set("FOLDED", "true")
    if children:
        for c in children:
            if isinstance(c, str):
                ET.SubElement(node, "node", TEXT=c)
            elif isinstance(c, tuple):
                add_node(node, c[0], c[1] if len(c) > 1 else None)
    return node

mm = ET.Element("map", version="1.0.1")
root = ET.SubElement(mm, "node", TEXT="马小i 多智能体 AI 系统")
root.set("STYLE", "bubble")

# ① 入口层
add_node(root, "① 入口层 Gateway", [
    ("FastAPI 网关 (app/main.py)", None),
    ("Vue3 SPA 前端 (web-ui/)", None),
    ("REST 路由注册", [
        "GET/POST /api/chat        → assistant graph 流式对话",
        "GET /api/agents           → 可委派智能体清单",
        "POST /api/docs/*          → 文档上传/入库/管理",
        "GET /api/files/*          → 文件下载",
        "GET/DELETE /api/memory/*  → 个人记忆自服务",
        "GET /api/kg/*             → 文档知识图谱",
        "POST /api/dataops/*       → 数据运维",
    ]),
])

# ② 调度核心
add_node(root, "② 调度核心 Assistant (LangGraph)", [
    ("编排节点流程", [
        "build_context   → resolve_time   (上下文拼装+时间注入)",
        "rewrite_query   → classify_intent (查询改写+意图分类)",
        "四路分发: kb_retrieve / kb_requery / kb_generate",
        "               tool_execute / agent_delegate",
        "               chitchat",
        "multi_agent_execute  (并发委派路径，跳过意图漏斗)",
        "persist_memory    (写回记忆)",
    ]),
    ("意图识别三层漏斗", [
        "L1  规则快筛: 正则/关键词, 0 ms, 无网络调用",
        "L2  bge-m3 语义分类: Top-K 均值 >= 0.62 且 margin >= 0.05",
        "L3  deepseek LLM 兜底",
        "终端 fallback: 关键词安全网",
    ]),
    ("多智能体并发委派", [
        "触发: ChatRequest.agent_targets 非空（前端多选）",
        "并发: asyncio.gather + Semaphore(2)",
        "超时: 外层 150s / 内层 send_guarded 120s",
        "合并: _merge_agent_answers → 一节一智能体 Markdown",
        "开关: MULTI_AGENT_ENABLED=false 回退单意图链路",
    ]),
])

# ③ RAG 知识底座
add_node(root, "③ RAG 知识底座", [
    ("混合检索", [
        "稠密向量: bge-m3 + pgvector HNSW (vector_cosine_ops)",
        "稀疏倒排: BM25 (Elasticsearch + jieba 中文分词)",
        "融合: RRF (Reciprocal Rank Fusion)",
    ]),
    ("重排序 Re-rank", [
        "bge-reranker-v2-m3 (真 cross-encoder)",
        "部署: compose 内 TEI 容器",
    ]),
    ("文档入库流水线", [
        "upload → parse(MinerU OCR) → chunk(结构化切块)",
        "→ embed(bge-m3) → pgvector upsert → ES 增量刷新",
        "→ 缓存失效通知",
    ]),
    ("文档级权限 RAG ACL", [
        "前置裁剪: 检索前按 user_id/tags 过滤 doc_key",
        "最终复核: 生成前二次核验",
    ]),
])

# ④ MCP 业务封装层
add_node(root, "④ MCP 业务封装层 (FastMCP)", [
    "hr-mcp           (port :18001)  人事工具集",
    "finance-mcp      (port :18002)  财务工具集",
    "analytics-mcp                   数据洞察工具集",
    "procurement-mcp                 采购工具集",
    "Text2SQL: PostgreSQL 方言 + sql_guard 黑名单",
    "同步 DB 访问: psycopg3 (app/db/sync.py)",
])

# ⑤ A2A 专业智能体
add_node(root, "⑤ A2A 专业智能体 (Agent2Agent)", [
    "HR Agent          人事专业智能体",
    "Finance Agent     财务专业智能体",
    "Analyst Agent     数据洞察专业智能体",
    "Contract Agent    合同专业智能体",
    "协议: JSON-RPC over HTTP (a2a-sdk)",
    "能力声明: agent_card.py",
    "路由: AGENT_URLS / AGENT_PROFILES 注册表 (a2a_client.py)",
])

# ⑥ 记忆层
add_node(root, "⑥ 记忆层", [
    ("会话记忆 Session Memory", [
        "Redis 滚动窗口 + 摘要压缩",
        "降级: 进程内 dict",
    ]),
    ("工作状态 Working State (Checkpointer)", [
        "AsyncRedisSaver (langgraph-checkpoint-redis)",
        "降级: InMemorySaver",
    ]),
    ("个人级六桶记忆 Personal Memory", [
        "User Memory     → user_profiles 表 (一人一条确定性合并)",
        "Preference      → long_term_memories (kind=preference)",
        "Habit           → long_term_memories (kind=habit)",
        "Episodic Memory → long_term_memories (kind=episodic, occurred_at)",
        "Personal Knowledge → long_term_memories (kind=knowledge)",
        "Personal Graph  → Neo4j (graph_store.py)",
        "桶语义单一事实源: taxonomy.py",
        "召回: build_context 一次 embedding 并行六桶",
        "写入: persist_memory 一次 LLM 调用分桶落盘",
    ]),
    ("长期记忆双通道", [
        "Vector 通道: pgvector (long_term_memories)",
        "Graph 通道: Neo4j (graph_store.py)",
    ]),
])

# ⑦ 缓存层
add_node(root, "⑦ 缓存层 Cache (统一 Redis)", [
    "Prompt Cache      LLM 系统提示词缓存",
    "Retrieval Cache   RAG 检索结果缓存",
    "Tool Cache        MCP 工具结果缓存 (仅只读工具，A2A 委派不缓存)",
    "全部路径可降级，Redis 不可用时直接穿透",
])

# ⑧ 安全治理体系
add_node(root, "⑧ 安全治理体系", [
    "身份认证 (auth.py)          Bearer Token + 角色白名单",
    "调用者上下文 (caller.py)    scope_id 多租户隔离",
    "文档级 ACL (acl.py)         用户-文档访问权限控制",
    "信息脱敏 (masking.py)       敏感字段自动遮盖",
    "全链路审计 (audit.py)       trace_id 串联，audit.jsonl 落盘",
    "配额控制 (quota.py)         请求速率限制",
    "URL/SSRF 防护 (url_guard.py)  外网抓取白名单",
    "SQL Guard (sql_guard.py)    黑名单语句 + SET LOCAL statement_timeout",
    "AST Guard (ast_guard.py)    Text2SQL AST 级白名单校验",
])

# ⑨ 文档知识图谱
add_node(root, "⑨ 文档知识图谱 GraphRAG", [
    "存储: Neo4j (:KgDoc / :KgEntity / [:KG_REL])",
    "抽取: LLM 关系提取 (app/kg/extract.py)",
    "受控词表: vocab.py (实体类型枚举 + 关系同义映射)",
    "边级 ACL: KG_REL.docs 字段记录来源文档",
    "  → 查询时按可访问 doc_key 即时裁剪",
    "前端: AntV G6 v5 力导向图 (GraphView.vue)",
])

# ⑩ 存储层
add_node(root, "⑩ 存储层 (三层分工)", [
    ("PostgreSQL + pgvector  [统一持久化]", [
        "documents / tags     文档元数据",
        "knowledge_chunks     知识块向量 (HNSW 索引)",
        "long_term_memories   长期记忆/个人记忆",
        "user_profiles        用户画像 (JSONB)",
        "hr_* / finance_*     业务数据表",
    ]),
    ("MongoDB  [正文外置]", [
        "raw_text / normalized_text / structure  整篇正文",
        "父块 context (按 parent_id 批量精确取)",
        "mongo_enabled=false 时父块降级为子块文本",
    ]),
    ("Elasticsearch  [BM25 派生倒排]", [
        "content_tokens       中文分词倒排索引",
        "可从 PG 全量 rebuild",
    ]),
    "Redis  Stack     缓存 + 会话记忆 + LangGraph Checkpointer",
    "Neo4j            文档知识图谱 + 个人记忆图谱",
])

# ⑪ 工具与能力
add_node(root, "⑪ 工具与能力", [
    ("联网搜索 search_web (app/tools/web.py)", [
        "deepseek Responses API (url_citation 引用)",
        "DDGS  DuckDuckGo 聚合搜索",
        "Serper  Google 搜索",
        "Tavily  LLM 优化索引",
        "开关: WEB_SEARCH_PROVIDER 配置切换",
    ]),
    ("文档生成 docgen (app/docgen/)", [
        "DOCX Builder (python-docx)",
        "PDF Builder  (weasyprint)",
        "PPTX Builder (python-pptx)",
        "XLSX Builder (openpyxl)",
        "Markdown Builder",
    ]),
    "数据分析 analytics (app/analytics/)",
    "文件管理 files (app/files/)",
])

# ⑫ 可观测性
add_node(root, "⑫ 可观测性 Observability", [
    "LangSmith Tracing (云端)",
    "Langfuse 自建可观测栈 (compose 内独立服务)",
    "trace_id 全链路串联 (网关→RAG→MCP→Agent)",
    "audit.jsonl 结构化审计日志",
    "能力开关摘要日志 (启动时打印 on/off 清单)",
])

# ⑬ 基础设施
add_node(root, "⑬ 基础设施 Docker Compose", [
    "assistant       :18000   FastAPI 网关",
    "postgres        :5432    pgvector/pg16",
    "redis           :6379    Redis Stack (含 search 模块)",
    "elasticsearch   :9200    BM25 倒排",
    "neo4j           :17474/17687  HTTP/Bolt",
    "mongodb         :27017   正文外置",
    "tei-reranker    :8080    bge-reranker-v2-m3",
    "mineru          :8002    OCR 文档解析",
    "hr-mcp          :18001",
    "finance-mcp     :18002",
    "analytics-mcp",
    "procurement-mcp",
    "hr-agent / finance-agent / analyst-agent / contract-agent",
    "langfuse (可选)  自建可观测",
    "密钥: docker/secrets/ 文件挂载, 不进 git/env",
])

# ---------- 写入文件 ----------
xml_str = minidom.parseString(
    ET.tostring(mm, encoding="unicode")
).toprettyxml(indent="  ", encoding=None)

# 去掉 minidom 加的多余 xml 声明行
lines = xml_str.splitlines()
if lines[0].startswith("<?xml"):
    lines = lines[1:]
out = '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(lines)

out_path = r"d:\ai\mxi\docs-interview\马小i系统架构图.mm"
with open(out_path, "w", encoding="utf-8") as f:
    f.write(out)

print("生成完成:", out_path)
