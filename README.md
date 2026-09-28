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
   └──── 存储: PostgreSQL(元数据 + 父/子块窄行 + pgvector 向量) + MongoDB(整篇 raw/normalized/structure + 父块全文) + Elasticsearch(BM25 派生倒排) + Redis(会话/缓存) + Neo4j(图谱) ────┘
   └──── 模型底座: Ollama (bge-m3 embedding) + TEI (bge-reranker-v2-m3 重排) + MinerU (图片OCR) ────┘
```

> 存储分工: PostgreSQL 仍是元数据与可索引窄行的事实来源(向量 / 文档与业务元数据 /
> ACL 标量列 / 子块 `chunk_text`); 正文本体(整篇 `raw/normalized/structure` 与父块全文)
> 外置到 MongoDB, 检索时按 `_id`/`parent_id` 精确批量取, 不做全文查询;
> Elasticsearch 仅作为可从主存全量重建的 BM25 派生倒排(只存 `content_tokens`, 不再存正文); Redis 承载会话/检查点/三类缓存; Neo4j 承载图谱。

A2A 遵循 Agent2Agent 协议:Agent Card 发布于 `/.well-known/agent-card.json`,
通信为 JSON-RPC 2.0 over HTTP(`message/send`)。MCP 遵循 Model Context
Protocol:FastMCP server,streamable-http transport(服务自身监听 `:8001/mcp`、`:8002/mcp`、
`:8005/mcp`、`:8006/mcp`;compose 已把它们发布到宿主 `18001`/`18002`/`18005`/`18006`,
宿主机直连走后者, 容器间走服务名+原端口)。

> 四个专业智能体与四个 MCP 域一一对应: `HR_Agent`(:9001)/`Finance_Agent`(:9002) 各走
> hr/finance 域; 新增 `Analyst_Agent`(:9005) 走 analytics 域(跨 HR/财务/采购的 Text2SQL
> 只读洞察 + SVG 图表 + 周期报告), `Contract_Agent`(:9006) 走 procurement 域(采购申请单
> 合规初审 + 合同条款初审, 规则引擎保底 + 模型补充语义风险)。分析产物落 `data/reports`,
> 由网关 `/api/files/reports/{name}` 静态回取。

### LangGraph 编排图

```
START
  │
  ▼
build_context ─────────── 汇聚 Business Context: 会话记忆(短期窗口 + 摘要),
  │                        个人级记忆六桶(画像/偏好/习惯/情节/知识/图谱)
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
                    persist_memory ────── 脱敏后写会话记忆 + 一次 LLM 提取,
                          │                分桶沉淀为个人记忆(溢出摘要另存情节)
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

### 重排序与置信阈值(真 cross-encoder)

RRF 只融合排名不判相关性, 全链路**唯一**的相关性阈值落在 rerank 阶段
(`app/rag/reranker.py` -> `HybridRetriever.retrieve`)：候选正文在 `attach_texts`
主键回表后就位, 一次性批量送 TEI 容器的 `BAAI/bge-reranker-v2-m3` 序列分类头打分,
输出 sigmoid 后的 **0~1 相关性**; 低于 `RETRIEVAL_SCORE_THRESHOLD`(默认 0.4) 的噪声
不进 Context Builder, 裁到空集即"未检索到相关文档", 由上层 judge 改写重检或明确拒答。

- 稠密/稀疏两通道与 RRF 均**不设阈值**(分数不可比, 只负责召回); TEI 不可用/超时
  (总超时 3s)则本层降级为 RRF 融合序且阈值不生效, `score_mode` 随分数标度一并上报审计。
- 推理交给独立的 TEI 容器(compose 服务 `tei-rerank`), 而不是 Ollama —— 之前的伪 rerank
  是拿 Ollama `/api/embed` 的向量做 cosine, 且该 GGUF 在 Windows llama.cpp 上调用即崩。

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

- **权限存储**:每条知识块记录(`doc_chunks` 子块表与 `doc_parents` 父块表)冗余携带
  `visibility / owner_id / dept_id / allowed_roles` 四个标量列,同库的
  `documents` 表为事实来源(`app/security/acl.py`);入库(`ingest`)与权限变更
  (`PUT /api/docs/{doc}/acl`)时两张表同步刷新(`update_acl_by_doc` 两条 UPDATE 同事务)。
  **ACL 永远以 PG/ES 标量列做前置裁剪, MongoDB 只按 `_id` 取文本, 不承载权限语义**;
  正文与 `extra`(JSONB) 只放展示字段, 权限字段禁止入 JSONB。
- **可见性策略**:`public`(全员) / `dept`(指定部门) / `role`(指定角色)
  / `private`(仅上传者);管理员角色全量可见。
- **前置裁剪**:稠密通道用 SQL 谓词(`app.rag.vectorstore.build_sql_filter`, 作用于
  `doc_chunks`), 与列白名单 `NARROW_COLUMNS`(不含 `chunk_text`/`embedding`) 一起作用在
  `ORDER BY embedding <=> ? LIMIT k` 之前, 在 ANN 检索阶段即排除无权文档且不拉宽行; 命中的窄行
  在 rerank 之前按 `chunk_id` 主键批量回表补正文。稀疏通道(Elasticsearch BM25)用等价的
  bool filter(`app.rag.bm25._acl_filter`), 两通道逐条语义严格一致。
- **最终授权**:`kb_answer` 在父块组装后、拼接 Context 前再逐条复核一次,
  拦截组装/脏数据可能引入的越权块,剔除项写审计(`acl_final_check_dropped`)。

## 目录结构

```
mxi/
├── app/
│   ├── config.py                 # 全局配置(pydantic-settings)
│   ├── schemas.py                # 共享数据模型(意图/角色/知识块)
│   ├── main.py                   # FastAPI 网关入口(有 web/dist 才托管 SPA, 否则提示走 vite dev)
│   ├── assistant/                # ★ Assistant 调度核心
│   │   ├── graph.py              #   LangGraph 编排:意图→分层路由→记忆
│   │   ├── intent.py             #   意图识别(deepseek-flash + 关键词兜底)
│   │   ├── memory.py             #   短期窗口 + LLM 摘要长期记忆
│   │   ├── mcp_client.py         #   MCP Client(langchain-mcp-adapters)
│   │   ├── a2a_client.py         #   A2A Client(Agent Card 发现/message.send)
│   │   └── router.py             #   /api/chat 统一入口
│   ├── memory/                   # ★ 个人级记忆层(按 user_id 隔离)
│   │   ├── taxonomy.py           #   分桶语义单一事实源(kind/注入方式/标签)
│   │   ├── personal.py           #   编排:读路径并行召回, 写路径分桶落盘
│   │   ├── profile_store.py      #   画像(user_profiles 表, 确定性合并)
│   │   ├── vector_store.py       #   偏好/习惯/情节/知识(pgvector 长表)
│   │   ├── graph_store.py        #   个人图谱(Neo4j, :MemoryUser 锚点)
│   │   ├── extraction.py         #   一次 LLM 调用产出全部桶
│   │   └── router.py             #   /api/memory 自服务(查看/删除/整理)
│   ├── rag/                      # ★ RAG 知识底座
│   │   ├── embeddings.py         #   bge-m3 (Ollama /api/embed)
│   │   ├── vectorstore.py        #   pgvector 父子双表(窄列 ANN + 主键回表 + ACL SQL 谓词)
│   │   ├── bm25.py               #   BM25 稀疏检索(Elasticsearch + jieba 预分词, 不存正文)
│   │   ├── reranker.py           #   TEI /rerank 真 cross-encoder (bge-reranker-v2-m3)
│   │   ├── retriever.py          #   混合检索(向量+BM25→RRF→回表→Rerank, 父块组装走 Mongo)
│   │   └── ingest.py             #   解析→归一化→父子拆分→expand-then-contract 发布
│   ├── bodies/                   # ★ 正文外置存储(MongoDB)
│   │   ├── client.py             #   连接与集合索引(doc_bodies/doc_body_parts/parent_texts)
│   │   └── store.py              #   正文门面: 整篇 raw/normalized/structure + 父块全文
│   ├── docs/                     # ★ 文档解析与元数据
│   │   ├── normalize.py          #   normalize_text/offset_slice/content_hash(offset 基准)
│   │   └── service.py            #   上传/入库(发布态门禁)/ACL/删除/列表
│   ├── mcp_servers/              # ★ MCP 工具层(业务系统封装)
│   │   ├── hr_server.py          #   HR 工单系统(:8001/mcp)
│   │   ├── finance_server.py     #   财务报销系统(:8002/mcp)
│   │   ├── analytics_server.py   #   数据洞察(:8005/mcp, Text2SQL/图表/周报)
│   │   └── procurement_server.py #   采购与合同初审(:8006/mcp)
│   ├── analytics/                # ★ 数据洞察支撑(零依赖, 不引 matplotlib)
│   │   ├── charts.py             #   纯 Python SVG 图表(bar/line/pie)
│   │   ├── reports.py            #   固定口径指标 SQL + Markdown 报告组装
│   │   └── store.py              #   产物落 data/reports + 台账 + 相对 URL 寻址
│   ├── procurement/              # ★ 采购/合同确定性规则引擎
│   │   └── rules.py              #   必备条款/高风险表述/金额分级/预算余额
│   ├── agents/                   # ★ A2A 专业智能体
│   │   ├── finance_agent/        #   agent_card / executor / server(:9002)
│   │   ├── hr_agent/             #   agent_card / executor / server(:9001)
│   │   ├── analyst_agent/        #   数据洞察 agent_card/executor/server(:9005)
│   │   └── contract_agent/       #   采购合同 agent_card/executor/server(:9006)
│   └── security/                 # ★ 安全治理
│       ├── auth.py               #   角色→工具/Agent 白名单
│       ├── acl.py                #   文档级 ACL(Principal→谓词/SQL/ES filter)
│       ├── audit.py              #   全链路审计 JSONL(trace_id 串联)
│       └── masking.py            #   身份证/银行卡/手机号/金额脱敏
├── scripts/
│   ├── dev_services.py           # docker 依赖服务一键起 + 对接自检(含密钥/串味守护)
│   ├── dev.ps1                   # 开发启动器: 依赖服务 + 宿主网关 + vite dev(-Stop 可停)
│   ├── package.py                # 一键打包前后端为部署目录(内含两道密钥闸门)
│   ├── ingest_knowledge.py       # 知识库构建脚本
│   ├── init_db.py                # PostgreSQL + pgvector 建表/自检
│   ├── migrate_doc_stores.py     # 存量迁移与校验: 正文入 Mongo + 父子拆双表
│   ├── bench_doc_stores.py       # 父子双表规模基准(只写独立评测库)
│   ├── test_sse_resume.py        # SSE 断点续流端到端用例(可走 vite 代理 MXI_BASE)
│   └── demo_reimburse.py         # 端到端 demo:我要报销
├── web-ui/                       # Vue3 前端源码(vite + element-plus)
│   └── vite.config.js            # dev 代理目标读 .env 的 ASSISTANT_PORT; build 输出 ../web/dist
├── web/dist/                     # 前端构建产物(仅生产/打包; 已 gitignore, dev 期不需要)
├── data/knowledge/               # 样例语料(制度 md + 培训视频字幕 srt)
├── docker/
│   ├── Dockerfile
│   ├── docker-compose.yml        # 服务编排 + 容器侧配置注入(env_file/environment)
│   ├── .env.example              # 容器侧配置模板(拷成 docker/.env)
│   ├── init/01_vector.sql        # PG 镜像启动时装 pgvector 扩展
│   ├── mineru/Dockerfile         # MinerU OCR 服务镜像
│   └── secrets/README.md         # 密钥存放说明(*.txt 不入库)
├── requirements.txt
├── .dockerignore                 # 密钥与大体积产物不进 build context
├── .env.example                  # 宿主侧配置模板(拷成 .env)
└── .env                          # 宿主侧真实配置(gitignore; 密钥不在这里)
```

## 快速开始

前置:LLM/意图识别走 DeepSeek 在线 API(需 API Key);本地 Ollama 仅用于
embedding;重排走独立的 TEI 容器服务;图片解析走 MinerU OCR 服务:

```bash
ollama pull bge-m3
```

重排权重需宿主机预下载一次(内网 huggingface.co 不可达, TEI 不会自下载; PowerShell):

```powershell
$env:HF_ENDPOINT='https://hf-mirror.com'; $env:HF_HUB_DISABLE_XET='1'
uvx --from "huggingface_hub<1" hf download BAAI/bge-reranker-v2-m3 `
  config.json model.safetensors sentencepiece.bpe.model `
  special_tokens_map.json tokenizer.json tokenizer_config.json `
  --local-dir data/tei_models/BAAI/bge-reranker-v2-m3
```

权重落 `data/tei_models/BAAI/bge-reranker-v2-m3/`(已 gitignore, 单文件 fp32 ~2.2GB, 勿用
`--exclude` 形式: hub 2.x CLI 会把它当文件名);compose 的 `tei-rerank` 服务以只读卷加载它。
无 NVIDIA GPU 时把 `docker/.env` 的 `TEI_IMAGE` 改成 `...:cpu-1.9` 即可。

### Docker Compose 启动(推荐)

首次部署需先有**两份东西**: 容器侧配置 `docker/.env`(从模板拷; compose 既用它插值, 又用
`env_file` 注入应用服务的真实环境变量, **缺文件会直接报错退出**)与密钥文件
(`docker/secrets/` 已被 .gitignore 排除, 不会进仓库):

```bash
cp .env.example .env                    # 宿主直跑网关才需要(密钥字段一律留空)
cp docker/.env.example docker/.env      # compose 强依赖: 插值源 + 容器侧 env_file
# 密钥只建在 docker/secrets/ 下(内容=单行裸值), 生成命令见 docker/secrets/README.md:
#   pg_password.txt        首次建库前生成; 必须与已有 pg_data 卷里的口令一致
#   deepseek_api_key.txt   LLM/意图识别用
```

然后拉起服务(`postgres` 以 `POSTGRES_PASSWORD_FILE=/run/secrets/pg_password` 读口令,
缺文件会在健康检查阶段就失败, 应用侧则由 `config.py` 回退读同名文件):

```bash
# 重排服务单独重启/看日志(权重未预下载时它会在加载模型阶段失败)
docker compose -f docker/docker-compose.yml up -d tei-rerank
# 建表 + pgvector 扩展(网关启动时也会自动完成, 这里显跑一次便于看报错)
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.init_db
# 正文外置存储: 启动 MongoDB(整篇正文 + 父块全文的存放层), assistant 已 depends_on mongo
docker compose -f docker/docker-compose.yml up -d mongo
# 存量迁移(首次或升级后): 正文入 Mongo + 父子拆双表; --verify-only 复核
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.migrate_doc_stores
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.migrate_doc_stores --verify-only
# 观察一个发布周期、--verify-only 通过后再下线旧表(改名而非 DROP, 可回滚):
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.migrate_doc_stores --drop-legacy
# 构建知识库(可选; 现在也可通过 Web 上传)
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.ingest_knowledge --dir /data/knowledge
# 重刷业务数据(可选)
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.seed_business_data --force
# Web 聊天: http://localhost:18000   文档管理: http://localhost:18000/upload
# Neo4j Browser: http://localhost:17474   (bolt: localhost:17687; 原 7687 已落进本机 winnat 排除段)
```

> **宿主发布端口对照(当前实测)**:
>
> | 服务                                                                                               | 宿主端口                                                                        | 容器内监听      | 说明                                          |
> | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | --------------- | --------------------------------------------- |
> | assistant                                                                                          | `18000`                                                                         | `8000`          | Web 聊天 / API / 文档管理                     |
> | hr-mcp / finance-mcp                                                                               | `18001` / `18002`                                                               | `8001` / `8002` | MCP 服务                                      |
> | analytics-mcp / procurement-mcp                                                                    | `18005` / `18006`                                                               | `8005` / `8006` | 数据洞察 / 采购合同 MCP                       |
> | neo4j HTTP                                                                                         | `17474`                                                                         | `7474`          | Neo4j Browser 网页控制台(人工看图谱时才需要)  |
> | neo4j Bolt                                                                                         | `17687`                                                                         | `7687`          | 驱动 RPC, 图记忆/文档知识图谱走这个(功能必需) |
> | tei-rerank                                                                                         | `8080`                                                                          | `8080`          | 重排服务 `/rerank`与`/health`                 |
> | postgres / ES / Redis / Mongo / mineru / hr-agent / finance-agent / analyst-agent / contract-agent | `5432` / `9200` / `6379` / `27017` / `8888` / `9001` / `9002` / `9005` / `9006` | 同左            | 未抬, 保持原值                                |
>
> 抬端口只动**宿主发布端口**, 容器内监听端口与 compose 网络内的服务名地址(`bolt://neo4j:7687` 等)
> 一律不变, 所以容器间连通性不受影响; 受影响的是宿主直连 —— 宿主 `.env` 必须写发布端口。
>
> 原因: Windows 的 `winnat`/Hyper-V 开机时会把整段端口写进"TCP 端口排除范围", 段内端口即使无人
> 监听也无法 bind, Docker 报 `ports are not available: ... forbidden by its access permissions`,
> 容器卡在 `Created`/`Exited(255)` 且**没有任何应用日志**。**排除段内容每次开机都会变**, 不要假设
> 某端口"一直能用": 本机 2026-09 两次实测分别为
> `7254-7353 / 7354-7453 / 7454-7553 / 7554-7653 / 7956-8055` 与
> `7137-7236 / 7237-7336 / 7530-7629 / 7630-7729` —— 后者把 `7687` 也括了进去。
> 排查命令: `netsh int ipv4 show excludedportrange protocol=tcp`。若某个服务因端口起不来,
> 其他容器里会报 `Failed to DNS resolve address <svc>:<port> ([Errno -2])` —— Docker 内置 DNS
> 不解析已退出容器的服务名, 根因不在报错那一层。
> 换机后若 `18xxx`/`17xxx` 也被排除, 改 `docker/.env` 里对应的 `*_HOST_PORT`/`TEI_PORT` 再抬一段
> 即可, 无需改 compose 默认值(见 `docker/docker-compose.yml` 顶部约定)。

### 本地开发

开发/验证期的拓扑约定(见 `CONFIG_RULES.md` 第 9 条):**宿主机只跑两个前台进程** ——
网关(`uvicorn --reload`)与前端(vite dev);compose 里有的服务全部对接 docker, 不在宿主
重复起一份。唯一不在 docker 里的依赖是宿主机 Ollama。

```bash
# 0. 依赖与环境(pip 仅用于引导 uv)
pip install uv
uv sync

# 1. 首次开发: 从模板建两份配置 + 密钥目录(模板入库, 真实值不入库)
cp .env.example .env                      # 宿主轨: 全部指向 docker 已发布端口
cp docker/.env.example docker/.env        # 容器轨: 服务名 + /data 卷路径 + compose 插值
# 密钥只写 docker/secrets/<name>.txt(见该目录 README.md), 不写进任何 .env

# 2. 建表自检(含 CREATE EXTENSION vector); Mongo 集合索引由网关启动幂等创建
uv run python -m scripts.init_db

# 3. 一键起: docker 依赖服务 + 对接自检 + 拉起网关(:18000) 与前端(:5173)
./scripts/dev.ps1
uv run python -m scripts.dev_services check        # 只看对接结果(幂等, 可随时跑)

# 4. 页面入口(开发期一律走 vite dev, 不依赖 web/dist)
#   聊天 http://localhost:5173   文档上传 http://localhost:5173/upload
#   记忆 http://localhost:5173/memory   知识图谱 http://localhost:5173/graph
#   API 文档 http://localhost:18000/docs   健康检查 http://localhost:18000/api/health

./scripts/dev.ps1 -Stop                            # 结束两个前台进程(docker 服务不动)
uv run python -m scripts.dev_services down         # 停掉 docker 依赖(只停不删卷)
```

`dev.ps1` 等价的手工四步(想单独控制某一层时用):

```bash
docker compose -f docker/docker-compose.yml up -d \
  postgres elasticsearch redis neo4j mongo tei-rerank mineru hr-mcp finance-mcp analytics-mcp procurement-mcp hr-agent finance-agent analyst-agent contract-agent
uv run python -m scripts.dev_services check
uv run uvicorn app.main:app --host 0.0.0.0 --port 18000 --reload   # 宿主网关(cd .venv 已激活或用 uv run)
cd web-ui && pnpm install && pnpm dev                              # 前端 dev, /api 代理到上面的端口
```

要点:

- **不起 `assistant` 容器**: 宿主网关要 bind `ASSISTANT_PORT`(默认 18000), 与容器发布端口互斥;
  整栈验证时才 `dev_services up --with-assistant`(此时宿主网关起不来)。
- **配置分两轨**: `app/config.py` 只读宿主侧 `.env`/`.env.local`; 容器侧靠 compose 的
  `env_file: [.env]`(= `docker/.env`) + `environment:` 注入。两侧地址不同是**故意的**,
  任何一侧混入另一侧的地址都会让对应层静默降级(见 `CONFIG_RULES.md` 第 5 条)。
- **降级是静默的**: Redis/Neo4j/TEI/Mongo/ES 连不上时功能照跑, 只是退回内存态或 RRF 融合序。
  所以改了配置先跑 `dev_services check`, 别看页面表现猜。
- **改过 MCP/A2A 代码要重建镜像**: docker 里跑的是镜像快照, 用 `./scripts/dev.ps1 -Build`。
- **SSE 在 dev 模式可用**: vite 代理是 http-proxy 非缓冲透传, `POST /api/chat/stream` 与断点续传
  都正常:`MXI_BASE=http://127.0.0.1:5173 uv run python scripts/test_sse_resume.py`。

> Windows 上 `8000`/`8001`/`8002`/`7474`/`7687` 若落在端口排除段内(见上节, 段内容每次开机都变),
> 宿主直跑也无法 bind; 因此宿主发布端口抬到 `18000/18001/18002/17474/17687`。换机后若再撞上,
> 改 `docker/.env` 的 `*_HOST_PORT` 与宿主 `.env` 的 `ASSISTANT_PORT`/`*_MCP_URL`/`NEO4J_URI` 即可
> (代码默认值在 `app/config.py`, 同步跟一下, 见 `CONFIG_RULES.md` 第 6 条)。

知识文件放宿主机 `data/knowledge/`,经 assistant 服务的 `../data:/data` 挂载映射为容器内
`/data/knowledge`。镜像 WORKDIR 是 `/srv`,写成 `--dir ./data/knowledge` 会解析到
`/srv/data/knowledge` —— 那是构建时 `COPY data/knowledge` 进去的快照,新增文件不重建镜像就读不到
(容器侧 `docker/.env` 已把 `KNOWLEDGE_DIR` 写成 `/data/knowledge`)。
`docker/data/knowledge/` 未被 compose 任何服务挂载(已作为遗留目录清理), 不要往那里放文件。

### 一键打包(前后端一体交付)

```bash
uv run python -m scripts.package                  # pnpm build + 组装部署目录 + zip
uv run python -m scripts.package --skip-frontend  # 复用现有 web/dist, 只重组包
uv run python -m scripts.package --check-only     # 只跑前置校验与密钥审计
uv run python -m scripts.package --strict-config  # 额外在包内跑 docker compose config 验证
uv run python -m scripts.package --tar            # 出 tar.gz(默认 zip)
```

产物: `build/mxi-deploy-<版本>-<git短sha>-<时间戳>/` 与同名压缩包, 含后端源码、前端构建产物
(`web/dist`, 由 `docker/Dockerfile` 的 `COPY web ./web` 打进镜像)、compose 编排、样例语料、
配置模板与 `DEPLOY.md`(目标机步骤)、`manifest.json`(文件清单 + sha256)。
按既定取舍**不含镜像**, 目标机自行 `docker compose ... up -d --build`。

两道密钥闸门(任一不过即中止且不产出包):

1. **源侧审计**: 本机 `.env`/`.env.local`/`docker/.env` 里出现明文密钥 → 拒绝打包(密钥应只在
   `docker/secrets/*.txt`)。
2. **产物扫描**: 对包内文本文件扫 `sk-` 前缀、私钥体、DSN 内嵌口令、云 AK、`key: 长值` →
   命中即中止。复制阶段另有硬黑名单(`.env`、`docker/.env`、`docker/secrets/*.txt`、上传件、
   模型权重、`node_modules` 等根本不进包), 扫描只是兜底。

### LangGraph Studio + LangSmith 可视化调试(仅本地开发)

tracing 默认关闭(`LANGSMITH_TRACING=false`, 代码默认值与容器侧配置都是 false), 对话数据不会上传;
以下能力仅在开发机显式开启。

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
