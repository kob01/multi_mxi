# 马小i · 企业级多智能体 AI 助手系统

参考马上消费"马小i"公开技术架构,实现 **Assistant-Agent 一入口多智能体** 模式:
用户只面对唯一 Assistant,由其按任务复杂度分层调度 —— 知识库直答(RAG)、
MCP 工具调用、A2A 专业智能体委派。

## 架构

```
                ┌──────────────────────── Web / API ─────────────────────────┐
                │                      Assistant (统一入口)                   │
                │  FastAPI + LangGraph 编排: 意图识别(deepseek-flash) → 分层路由 │
                └───┬───────────────┬───────────────────┬────────────────────┘
                    │               │                   │
            a. 简单查询      b. 复杂操作(MCP)      c. 专业任务(A2A)
                    │               │                   │
          ┌─────────▼────┐  ┌───────▼───────┐   ┌───────▼────────┐
          │  RAG 知识底座 │  │  MCP Servers  │   │ 专业 Agent      │
          │ bge-m3 向量  │  │ HR工单 :8001  │   │ HR_Agent :9001 │
          │ pgvector     │  │ 财务报销:8002 │   │ Finance  :9002 │
          │ BM25+Rerank  │  │               │   │ (LangGraph+MCP)│
          │ +Metadata ACL│  │               │   │                │
          └──────────────┘  └───────────────┘   └────────────────┘
                    ├──── LLM: DeepSeek API (deepseek-flash) ────┤
   └──── 存储: PostgreSQL(元数据 + pgvector 知识块) + Elasticsearch(BM25 稀疏通道的倒排索引) ────┘
   └──── 模型底座: Ollama (bge-m3 / bge-reranker-v2-m3) + MinerU (图片OCR) ────┘
```

> 存储层统一为一个 PostgreSQL 实例: 文档/业务元数据与向量知识块同库同连接池
> (原 MySQL + Milvus Lite 的组合已合并), Elasticsearch 仅作为可全量重建的 BM25
> 派生索引保留。

A2A 遵循 Agent2Agent 协议:Agent Card 发布于 `/.well-known/agent-card.json`,
通信为 JSON-RPC 2.0 over HTTP(`message/send`)。MCP 遵循 Model Context
Protocol:FastMCP server,streamable-http transport(`:8001/mcp`、`:8002/mcp`)。

### LangGraph 编排图

```
START
  │
  ▼
load_context ──────────── 载入会话记忆(短期窗口 + 长期摘要)
  │
  ▼
resolve_time ──────────── 问题含相对时间时预取平台时钟(东八区),
  │                        注入后续四类路由 Prompt, 不依赖模型记忆日期
  ▼
rewrite_query ─────────── 多轮指代消解: 把"那它的劣势呢"改写为独立查询
  │                        (首轮/无历史/自包含长问题跳过; 输出清洗+校验,
  │                        LLM 失败或结果不可信时回退原话)
  ▼
classify_intent ───────── deepseek-flash 意图识别(基于消解后的独立问题,
  │                        关键词兜底)
  │
  ├── knowledge_qa ──► kb_answer ─────── RAG 混合检索(用消解后的问题检索,
  │                       │                生成与检索语义对齐)
  ├── chitchat ──────► chitchat ──────── 直答(携带对话历史 + 消解提示)
  ├── tool_call ─────► tool_execute ──── MCP ReAct 工具调用(消解后的问题
  │                       │                驱动, 角色×工具白名单过滤)
  └── agent_delegate ► agent_delegate ── A2A 委派专业智能体(消解后的问题
                          │                作为当前请求, metadata 传身份)
                          ▼
                    persist_memory ────── 脱敏后写入会话记忆
                          │                + 全链路审计(同一 trace_id)
                          ▼
                         END
```

要点:
- `resolve_time` 在意图识别**之前**固定执行,保证时间类回答以真实时钟为准;
- `rewrite_query` 前置于意图识别:分类器与**全部四条路由**共享消解后的
  独立问题,避免"那帮我查一下它的余额"因指代未消解而误分类/查错对象;
  首轮、无历史或自包含长问题(无代词标记)自动跳过,零额外开销;
- 身份与用户请求文本分离:工具调用经 System 消息、A2A 经协议级 metadata
  下发操作者身份,并显式区分"当前操作者"与"任务目标用户";
- 每个节点均写审计记录,同一 `trace_id` 串联全链路。

### 文档级权限控制(RAG ACL)

知识库检索在语义链路之外叠加一条**授权链路**:文档权限以元数据形式存储,
用户身份来自统一身份系统,检索时经 Metadata Filter 前置裁剪,进入 LLM 前
再做一次授权复核。

```
用户(user_id / department / role)
   │
   ▼
Authorization Service ── Principal(user_id, department, role)
   │  ACL Filter
   ▼
RAG:  Query → Embedding
        ▼
      Vector/BM25 Search  +  Permission Filter   ← 前置裁剪(无权文档不进候选集)
        ▼
      TopK → Rerank
        ▼
   Final Authorization   ← 进入 Context Builder 前逐条复核(纵深防御)
        ▼
   Context Builder → LLM
```

- **权限存储**:每条知识块记录(`knowledge_chunks` 表)冗余携带
  `visibility / owner_id / dept_id / allowed_roles` 四个标量列,同库的
  `documents` 表为事实来源(`app/security/acl.py`);入库(`ingest`)与权限变更
  (`PUT /api/docs/{doc}/acl`)时同步刷新向量表与 BM25 通道。
- **可见性策略**:`public`(全员) / `dept`(指定部门) / `role`(指定角色)
  / `private`(仅上传者);管理员角色全量可见。
- **前置裁剪**:稠密通道用 SQL 谓词(`app.rag.vectorstore.build_sql_filter`,
  与 `is_parent = false` 一起作用在 `ORDER BY embedding <=> ? LIMIT k` 之前),
  在 ANN 检索阶段即排除无权文档,不占用 TopK;稀疏通道(Elasticsearch BM25)
  用等价的 bool filter(`app.rag.bm25._acl_filter`),两通道语义严格一致。
- **最终授权**:`kb_answer` 在父块组装后、拼接 Context 前再逐条复核一次,
  拦截组装/脏数据可能引入的越权块,剔除项写审计(`acl_final_check_dropped`)。

## 目录结构

```
mxi/
├── app/
│   ├── config.py                 # 全局配置(pydantic-settings)
│   ├── schemas.py                # 共享数据模型(意图/角色/知识块)
│   ├── main.py                   # FastAPI 网关入口(挂载 Web UI)
│   ├── assistant/                # ★ Assistant 调度核心
│   │   ├── graph.py              #   LangGraph 编排:意图→分层路由→记忆
│   │   ├── intent.py             #   意图识别(deepseek-flash + 关键词兜底)
│   │   ├── memory.py             #   短期窗口 + LLM 摘要长期记忆
│   │   ├── mcp_client.py         #   MCP Client(langchain-mcp-adapters)
│   │   ├── a2a_client.py         #   A2A Client(Agent Card 发现/message.send)
│   │   └── router.py             #   /api/chat 统一入口
│   ├── rag/                      # ★ RAG 知识底座
│   │   ├── embeddings.py         #   bge-m3 (Ollama /api/embed)
│   │   ├── vectorstore.py        #   pgvector 知识块表(ANN 检索 + ACL SQL 谓词)
│   │   ├── bm25.py               #   BM25 稀疏检索(Elasticsearch + jieba 预分词)
│   │   ├── reranker.py           #   bge-reranker-v2-m3 重排
│   │   ├── retriever.py          #   混合检索(向量+BM25→RRF→Rerank)
│   │   └── ingest.py             #   解析(txt/md/pdf/docx/视频字幕)→切分→入库
│   ├── mcp_servers/              # ★ MCP 工具层(业务系统封装)
│   │   ├── hr_server.py          #   HR 工单系统(:8001/mcp)
│   │   └── finance_server.py     #   财务报销系统(:8002/mcp)
│   ├── agents/                   # ★ A2A 专业智能体
│   │   ├── finance_agent/        #   agent_card / executor / server(:9002)
│   │   └── hr_agent/             #   agent_card / executor / server(:9001)
│   └── security/                 # ★ 安全治理
│       ├── auth.py               #   角色→工具/Agent 白名单
│       ├── acl.py                #   文档级 ACL(Principal→谓词/SQL/ES filter)
│       ├── audit.py              #   全链路审计 JSONL(trace_id 串联)
│       └── masking.py            #   身份证/银行卡/手机号/金额脱敏
├── scripts/
│   ├── ingest_knowledge.py       # 知识库构建脚本
│   ├── init_db.py                # PostgreSQL + pgvector 建表/自检
│   ├── migrate_mxi_storage.py    # 一次性迁移: MySQL + Milvus Lite -> PostgreSQL
│   └── demo_reimburse.py         # 端到端 demo:我要报销
├── data/knowledge/               # 样例语料(制度 md + 培训视频字幕 srt)
├── web/index.html                # Web 聊天界面
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml        # 一键启动
├── requirements.txt
└── .env
```

## 快速开始

前置:LLM/意图识别走 DeepSeek 在线 API(需 API Key);本地 Ollama 仅用于
embedding/rerank 模型;图片解析走 MinerU OCR 服务:

```bash
ollama pull bge-m3
ollama pull dengcao/bge-reranker-v2-m3
```

MinerU 图片解析(上传 jpg/png 等图片时需要),任选其一:

```bash
# 方式 A: Docker Compose 内置服务(推荐, 本地构建 docker/mineru/Dockerfile,
# 已补齐 slim 镜像缺失的 cv2 系统库 libxcb/libGL 等; 首次启动自动从 ModelScope 下载模型)
docker compose -f docker/docker-compose.yml --profile mineru up -d mineru
# 方式 B: pip 安装 (Python 3.10+, GPU 可选 vlm 后端)
pip install "mineru[core]"
mineru-api --host 0.0.0.0 --port 8888
```

(默认 `.env` 的 `MINERU_BASE_URL=http://host.docker.internal:8888` 指向宿主机端口,
三种方式均可被容器内 assistant 访问)

### Docker Compose 启动(推荐)

compose 里的 `postgres` 服务以 secret 文件读取数据库密码, 首次部署需先创建它
(`docker/secrets/` 已被 .gitignore 排除, 不会进仓库):

```bash
mkdir -p docker/secrets && echo '<你的 PG 密码>' > docker/secrets/pg_password.txt
cp .env docker/.env   # 然后按容器语义改: PG_HOST=postgres、OLLAMA_BASE_URL=host.docker.internal
docker compose -f docker/docker-compose.yml up -d --build
# 建表 + pgvector 扩展(网关启动时也会自动完成, 这里显跑一次便于看报错)
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.init_db
# 构建知识库(可选; 现在也可通过 Web 上传)
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.ingest_knowledge --dir /data/knowledge
# 重刷业务数据(可选)
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.seed_business_data --force
# Web 聊天: http://localhost:8000   文档管理: http://localhost:8000/upload
```

### 从 MySQL + Milvus Lite 迁移存量数据

一次性脚本把旧元数据/业务表与旧 Milvus 向量块都灌进 PostgreSQL(可重跑):

```bash
# 1) 起 PG 并建好 schema
uv run python -m scripts.init_db

# 2) 搬数据 (脚本需要临时读 MySQL, 所以临时带上 pymysql/pymilvus)
uv run --with pymysql --with pymilvus python -m scripts.migrate_mxi_storage \
    --mysql-url "mysql+pymysql://user:pw@host:3306/dbname?charset=utf8mb4" \
    --milvus-uri ./data/milvus_lite.db

# 不想搬向量 (或旧 collection 已被清): 只搬元数据, 向量重新入库
uv run --with pymysql python -m scripts.migrate_mxi_storage --mysql-url "..." --skip-vectors
uv run python -m scripts.ingest_knowledge --dir ./data/knowledge

# 3) 校验行数一致后重刷 ES 派生索引, 确认无误再删除 data/milvus_lite.db/
```

旧向量列含 `is_parent=1` 的父块也会一并搬迁(检索不命中它们, 仅用于父块组装)。
若单文档块数超过 Milvus 单次 query 上限(16384), 脚本会打 WARNING, 该文档建议重新入库。

### 本地开发

```bash
# 建议用 uv 管理依赖
pip install uv
uv sync

# 配置敏感信息: 在项目根目录 .env 填写 PG_PASSWORD=... 与 DEEPSEEK_API_KEY=...
# (该文件已被 .gitignore 排除); 也可直接设置环境变量 (PowerShell: $env:DEEPSEEK_API_KEY="...")

# 本地需先有一个带 pgvector 扩展的 PostgreSQL (推荐容器):
#   docker compose -f docker/docker-compose.yml up -d postgres

# 1. 建表自检(含 CREATE EXTENSION vector)
uv run python -m scripts.init_db

# 2. 启动业务 MCP / A2A 服务(按需)
uv run python -m app.mcp_servers.hr_server &        # :8001
uv run python -m app.mcp_servers.finance_server &   # :8002
uv run python -m app.agents.hr_agent.server &       # :9001
uv run python -m app.agents.finance_agent.server &  # :9002

# 3. 启动 Assistant 网关
uv run uvicorn app.main:app --port 8000

# Web 聊天: http://localhost:8000
# 文档上传/管理: http://localhost:8000/upload
# 命令行方式构建知识库(首次或批量):
uv run python -m scripts.ingest_knowledge --dir ./data/knowledge
```

### LangGraph Studio + LangSmith 可视化调试(仅本地开发)

生产容器默认关闭 tracing(`LANGSMITH_TRACING=false`), 对话数据不会上传; 以下能力仅在开发机启用。

```bash
# 1. 在 .env 中开启并填入你在 https://smith.langchain.com 申请的密钥
#    LANGSMITH_TRACING=true
#    LANGSMITH_API_KEY=ls_...

# 2. 启动 Studio 本地 dev server (图定义见 langgraph.json -> assistant)
uv run langgraph dev
# 自动打开浏览器进入 LangGraph Studio, 可可视化编辑/运行编排图、
# 单节点调试、断点回放; 每次运行同时作为 trace 上报 LangSmith 项目 mxi-assistant
```

- `uv sync` 已自动安装 dev 组依赖(`langgraph-cli[inmem]`), 不会进入生产镜像(Dockerfile 用 `--no-dev`)。
- LangSmith trace 与现有 `audit.jsonl` 全链路审计互补: 前者面向开发调试/评估, 后者面向合规留痕。

## 端到端链路("我要报销")

1. `POST /api/chat` → Assistant 载入会话记忆(短期窗口 + 长期摘要);问题含相对时间时,
   先在进程内取平台当前时间并注入后续 Prompt
2. `deepseek-flash` 意图识别 → `agent_delegate / finance`
3. 权限校验(角色白名单)→ A2A Client 拉取 Finance_Agent 的 Agent Card 并 `message/send`
4. Finance_Agent(LangGraph ReAct + deepseek-flash)追问/补齐要素后,经 MCP 调用
   `create_reimbursement` 创建报销单
5. 单号/审批节点沿 A2A 返回 → Assistant 回复用户;全程写 `logs/audit.jsonl`
   (同一 trace_id),敏感字段(金额/证件号/手机号)脱敏。

K8s 部署:将 `docker/docker-compose.yml` 中 5 个服务各映射为 Deployment+Service
(compose 可用 `kompose convert` 直接转换),Ollama 建议独立部署为推理服务,
`OLLAMA_BASE_URL` 指向其集群内 Service 地址即可,应用代码无需改动。
