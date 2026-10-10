<?xml version="1.0" encoding="UTF-8"?>
<map version="1.0.1">
  <node TEXT="马小i 多智能体 AI 系统" STYLE="bubble">
    <node TEXT="① 入口层 Gateway">
      <node TEXT="FastAPI 网关 (app/main.py)"/>
      <node TEXT="Vue3 SPA 前端 (web-ui/)"/>
      <node TEXT="REST 路由注册">
        <node TEXT="GET/POST /api/chat        → assistant graph 流式对话"/>
        <node TEXT="GET /api/agents           → 可委派智能体清单"/>
        <node TEXT="POST /api/docs/*          → 文档上传/入库/管理"/>
        <node TEXT="GET /api/files/*          → 文件下载"/>
        <node TEXT="GET/DELETE /api/memory/*  → 个人记忆自服务"/>
        <node TEXT="GET /api/kg/*             → 文档知识图谱"/>
        <node TEXT="POST /api/dataops/*       → 数据运维"/>
      </node>
    </node>
    <node TEXT="② 调度核心 Assistant (LangGraph)">
      <node TEXT="编排节点流程">
        <node TEXT="build_context   → resolve_time   (上下文拼装+时间注入)"/>
        <node TEXT="rewrite_query   → classify_intent (查询改写+意图分类)"/>
        <node TEXT="四路分发: kb_retrieve / kb_requery / kb_generate"/>
        <node TEXT="               tool_execute / agent_delegate"/>
        <node TEXT="               chitchat"/>
        <node TEXT="multi_agent_execute  (并发委派路径，跳过意图漏斗)"/>
        <node TEXT="persist_memory    (写回记忆)"/>
      </node>
      <node TEXT="意图识别三层漏斗">
        <node TEXT="L1  规则快筛: 正则/关键词, 0 ms, 无网络调用"/>
        <node TEXT="L2  bge-m3 语义分类: Top-K 均值 &gt;= 0.62 且 margin &gt;= 0.05"/>
        <node TEXT="L3  deepseek LLM 兜底"/>
        <node TEXT="终端 fallback: 关键词安全网"/>
      </node>
      <node TEXT="多智能体并发委派">
        <node TEXT="触发: ChatRequest.agent_targets 非空（前端多选）"/>
        <node TEXT="并发: asyncio.gather + Semaphore(2)"/>
        <node TEXT="超时: 外层 150s / 内层 send_guarded 120s"/>
        <node TEXT="合并: _merge_agent_answers → 一节一智能体 Markdown"/>
        <node TEXT="开关: MULTI_AGENT_ENABLED=false 回退单意图链路"/>
      </node>
    </node>
    <node TEXT="③ RAG 知识底座">
      <node TEXT="混合检索">
        <node TEXT="稠密向量: bge-m3 + pgvector HNSW (vector_cosine_ops)"/>
        <node TEXT="稀疏倒排: BM25 (Elasticsearch + jieba 中文分词)"/>
        <node TEXT="融合: RRF (Reciprocal Rank Fusion)"/>
      </node>
      <node TEXT="重排序 Re-rank">
        <node TEXT="bge-reranker-v2-m3 (真 cross-encoder)"/>
        <node TEXT="部署: compose 内 TEI 容器"/>
      </node>
      <node TEXT="文档入库流水线">
        <node TEXT="upload → parse(MinerU OCR) → chunk(结构化切块)"/>
        <node TEXT="→ embed(bge-m3) → pgvector upsert → ES 增量刷新"/>
        <node TEXT="→ 缓存失效通知"/>
      </node>
      <node TEXT="文档级权限 RAG ACL">
        <node TEXT="前置裁剪: 检索前按 user_id/tags 过滤 doc_key"/>
        <node TEXT="最终复核: 生成前二次核验"/>
      </node>
    </node>
    <node TEXT="④ MCP 业务封装层 (FastMCP)">
      <node TEXT="hr-mcp           (port :18001)  人事工具集"/>
      <node TEXT="finance-mcp      (port :18002)  财务工具集"/>
      <node TEXT="analytics-mcp                   数据洞察工具集"/>
      <node TEXT="procurement-mcp                 采购工具集"/>
      <node TEXT="Text2SQL: PostgreSQL 方言 + sql_guard 黑名单"/>
      <node TEXT="同步 DB 访问: psycopg3 (app/db/sync.py)"/>
    </node>
    <node TEXT="⑤ A2A 专业智能体 (Agent2Agent)">
      <node TEXT="HR Agent          人事专业智能体"/>
      <node TEXT="Finance Agent     财务专业智能体"/>
      <node TEXT="Analyst Agent     数据洞察专业智能体"/>
      <node TEXT="Contract Agent    合同专业智能体"/>
      <node TEXT="协议: JSON-RPC over HTTP (a2a-sdk)"/>
      <node TEXT="能力声明: agent_card.py"/>
      <node TEXT="路由: AGENT_URLS / AGENT_PROFILES 注册表 (a2a_client.py)"/>
    </node>
    <node TEXT="⑥ 记忆层">
      <node TEXT="会话记忆 Session Memory">
        <node TEXT="Redis 滚动窗口 + 摘要压缩"/>
        <node TEXT="降级: 进程内 dict"/>
      </node>
      <node TEXT="工作状态 Working State (Checkpointer)">
        <node TEXT="AsyncRedisSaver (langgraph-checkpoint-redis)"/>
        <node TEXT="降级: InMemorySaver"/>
      </node>
      <node TEXT="个人级六桶记忆 Personal Memory">
        <node TEXT="User Memory     → user_profiles 表 (一人一条确定性合并)"/>
        <node TEXT="Preference      → long_term_memories (kind=preference)"/>
        <node TEXT="Habit           → long_term_memories (kind=habit)"/>
        <node TEXT="Episodic Memory → long_term_memories (kind=episodic, occurred_at)"/>
        <node TEXT="Personal Knowledge → long_term_memories (kind=knowledge)"/>
        <node TEXT="Personal Graph  → Neo4j (graph_store.py)"/>
        <node TEXT="桶语义单一事实源: taxonomy.py"/>
        <node TEXT="召回: build_context 一次 embedding 并行六桶"/>
        <node TEXT="写入: persist_memory 一次 LLM 调用分桶落盘"/>
      </node>
      <node TEXT="长期记忆双通道">
        <node TEXT="Vector 通道: pgvector (long_term_memories)"/>
        <node TEXT="Graph 通道: Neo4j (graph_store.py)"/>
      </node>
    </node>
    <node TEXT="⑦ 缓存层 Cache (统一 Redis)">
      <node TEXT="Prompt Cache      LLM 系统提示词缓存"/>
      <node TEXT="Retrieval Cache   RAG 检索结果缓存"/>
      <node TEXT="Tool Cache        MCP 工具结果缓存 (仅只读工具，A2A 委派不缓存)"/>
      <node TEXT="全部路径可降级，Redis 不可用时直接穿透"/>
    </node>
    <node TEXT="⑧ 安全治理体系">
      <node TEXT="身份认证 (auth.py)          Bearer Token + 角色白名单"/>
      <node TEXT="调用者上下文 (caller.py)    scope_id 多租户隔离"/>
      <node TEXT="文档级 ACL (acl.py)         用户-文档访问权限控制"/>
      <node TEXT="信息脱敏 (masking.py)       敏感字段自动遮盖"/>
      <node TEXT="全链路审计 (audit.py)       trace_id 串联，audit.jsonl 落盘"/>
      <node TEXT="配额控制 (quota.py)         请求速率限制"/>
      <node TEXT="URL/SSRF 防护 (url_guard.py)  外网抓取白名单"/>
      <node TEXT="SQL Guard (sql_guard.py)    黑名单语句 + SET LOCAL statement_timeout"/>
      <node TEXT="AST Guard (ast_guard.py)    Text2SQL AST 级白名单校验"/>
    </node>
    <node TEXT="⑨ 文档知识图谱 GraphRAG">
      <node TEXT="存储: Neo4j (:KgDoc / :KgEntity / [:KG_REL])"/>
      <node TEXT="抽取: LLM 关系提取 (app/kg/extract.py)"/>
      <node TEXT="受控词表: vocab.py (实体类型枚举 + 关系同义映射)"/>
      <node TEXT="边级 ACL: KG_REL.docs 字段记录来源文档"/>
      <node TEXT="  → 查询时按可访问 doc_key 即时裁剪"/>
      <node TEXT="前端: AntV G6 v5 力导向图 (GraphView.vue)"/>
    </node>
    <node TEXT="⑩ 存储层 (三层分工)">
      <node TEXT="PostgreSQL + pgvector  [统一持久化]">
        <node TEXT="documents / tags     文档元数据"/>
        <node TEXT="knowledge_chunks     知识块向量 (HNSW 索引)"/>
        <node TEXT="long_term_memories   长期记忆/个人记忆"/>
        <node TEXT="user_profiles        用户画像 (JSONB)"/>
        <node TEXT="hr_* / finance_*     业务数据表"/>
      </node>
      <node TEXT="MongoDB  [正文外置]">
        <node TEXT="raw_text / normalized_text / structure  整篇正文"/>
        <node TEXT="父块 context (按 parent_id 批量精确取)"/>
        <node TEXT="mongo_enabled=false 时父块降级为子块文本"/>
      </node>
      <node TEXT="Elasticsearch  [BM25 派生倒排]">
        <node TEXT="content_tokens       中文分词倒排索引"/>
        <node TEXT="可从 PG 全量 rebuild"/>
      </node>
      <node TEXT="Redis  Stack     缓存 + 会话记忆 + LangGraph Checkpointer"/>
      <node TEXT="Neo4j            文档知识图谱 + 个人记忆图谱"/>
    </node>
    <node TEXT="⑪ 工具与能力">
      <node TEXT="联网搜索 search_web (app/tools/web.py)">
        <node TEXT="deepseek Responses API (url_citation 引用)"/>
        <node TEXT="DDGS  DuckDuckGo 聚合搜索"/>
        <node TEXT="Serper  Google 搜索"/>
        <node TEXT="Tavily  LLM 优化索引"/>
        <node TEXT="开关: WEB_SEARCH_PROVIDER 配置切换"/>
      </node>
      <node TEXT="文档生成 docgen (app/docgen/)">
        <node TEXT="DOCX Builder (python-docx)"/>
        <node TEXT="PDF Builder  (weasyprint)"/>
        <node TEXT="PPTX Builder (python-pptx)"/>
        <node TEXT="XLSX Builder (openpyxl)"/>
        <node TEXT="Markdown Builder"/>
      </node>
      <node TEXT="数据分析 analytics (app/analytics/)"/>
      <node TEXT="文件管理 files (app/files/)"/>
    </node>
    <node TEXT="⑫ 可观测性 Observability">
      <node TEXT="LangSmith Tracing (云端)"/>
      <node TEXT="Langfuse 自建可观测栈 (compose 内独立服务)"/>
      <node TEXT="trace_id 全链路串联 (网关→RAG→MCP→Agent)"/>
      <node TEXT="audit.jsonl 结构化审计日志"/>
      <node TEXT="能力开关摘要日志 (启动时打印 on/off 清单)"/>
    </node>
    <node TEXT="⑬ 基础设施 Docker Compose">
      <node TEXT="assistant       :18000   FastAPI 网关"/>
      <node TEXT="postgres        :5432    pgvector/pg16"/>
      <node TEXT="redis           :6379    Redis Stack (含 search 模块)"/>
      <node TEXT="elasticsearch   :9200    BM25 倒排"/>
      <node TEXT="neo4j           :17474/17687  HTTP/Bolt"/>
      <node TEXT="mongodb         :27017   正文外置"/>
      <node TEXT="tei-reranker    :8080    bge-reranker-v2-m3"/>
      <node TEXT="mineru          :8002    OCR 文档解析"/>
      <node TEXT="hr-mcp          :18001"/>
      <node TEXT="finance-mcp     :18002"/>
      <node TEXT="analytics-mcp"/>
      <node TEXT="procurement-mcp"/>
      <node TEXT="hr-agent / finance-agent / analyst-agent / contract-agent"/>
      <node TEXT="langfuse (可选)  自建可观测"/>
      <node TEXT="密钥: docker/secrets/ 文件挂载, 不进 git/env"/>
    </node>
  </node>
</map>