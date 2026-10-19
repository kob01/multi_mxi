# 马小i · 企业级多智能体 AI 助手系统

参考马上消费"马小i"公开技术架构,实现 **Assistant-Agent 一入口多智能体** 模式:
用户只面对唯一 Assistant,由其按任务复杂度分层调度 —— 知识库直答(RAG)、
MCP 工具调用、A2A 专业智能体委派。

## 架构

```
                ┌──────────────────────── Web / API ─────────────────────────┐
                │                      Assistant (统一入口)                   │
                │  FastAPI + LangGraph 编排: 消解 → 意图三层漏斗 → 分层路由/并发委派  │
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
`:8005/mcp`、`:8006/mcp`、`:8007/mcp`;compose 已把它们发布到宿主 `18001`/`18002`/`18005`/`18006`/`18007`,
宿主机直连走后者, 容器间走服务名+原端口)。

> 五个专业智能体与五个 MCP 域一一对应: `HR_Agent`(:9001)/`Finance_Agent`(:9002) 各走
> hr/finance 域; `Analyst_Agent`(:9005) 走 analytics 域(跨 HR/财务/采购的 Text2SQL
> 只读洞察 + SVG/PNG 图表 + 周期报告), `Contract_Agent`(:9006) 走 procurement 域(采购申请单
> 合规初审 + 合同条款初审, 规则引擎保底 + 模型补充语义风险), `AskData_Agent`(:9007) 走 askdata
> 域(智能问数: NL2DSL + 语义层, 多维下钻与同环比由引擎算)。
> 分析产物(图表/报告)落 `data/reports`, 由网关 `/api/files/reports/{name}` 静态回取。
>
> **Analyst 与 AskData 的分界不是重复建设**: Analyst_Agent 是自由 Text2SQL(模型写 SELECT,
> 服务端只做拦截), 适合口径未被治理过的探索式分析与成文报告; AskData_Agent 禁止模型出 SQL,
> 只能选语义层已登记的指标与维度(SQL 由服务端模板编译), 适合"要拿准数"且必须可复现口径的
> 场景。同一名词的口径两边共用 `app/analytics/reports.py` 的同一份常量, 不会出现"周报与问数
> 两个数对不上账"。
>
> 另有一组**进程内工具能力域**(`app/tools/`, 无容器/端口): `web`(联网检索 `search_web` +
> 网页抓取 `fetch_url`, SSRF 护栏见 `app/security/url_guard.py`)与 `docgen`(把结构化文字 +
> 图片直接生成为可下载的 Word/Excel/PPT/PDF/Markdown 文件, `generate_docx/xlsx/pptx/pdf/md/image`
> 回 `/api/files/{token}/{file}` 下载链接; 图片支持本地文件与图片 URL)。docgen 域还**并入了
> `search_web`/`fetch_url`** —— 否则"调研 X 再导出 PDF"只能靠模型记忆编内容。它们由
> `tool_execute` 按 `CAPABILITY_TOOLS` 注册表直接注入 ReAct 循环, 不经 MCP 连接池;
> 检索默认免密 provider `ddgs`, Tavily/Serper 密钥只住 `docker/secrets/*.txt`。
>
> 注: 原"网页创作工坊"(HTML 成品页 + Playwright 渲染沙箱 `docgen-sandbox` + 编辑器)
> 已整体下线 —— docgen 的目标就是"文字+图片直接产出可下载文档", 不再有网页这条岔路。

### LangGraph 编排图

```
START
  │
  ▼
build_context ─────────── 汇聚 Business Context: 会话记忆(短期窗口 + 摘要),
  │                        个人级记忆六桶(画像/偏好/习惯/情节/知识/图谱;
  │                        五桶中画像走 user_profiles 一人一条, 其余四个走
  │                        long_term_memories 的 kind 列, 图谱在 Neo4j)
  ▼
resolve_time ──────────── 问题含相对时间时预取平台时钟(东八区),
  │                        注入后续五类路由 Prompt, 不依赖模型记忆日期
  ▼
rewrite_query ─────────── 多轮指代消解: 把"那它的劣势呢"改写为独立查询
  │                        (首轮/无历史/自包含长问题跳过; 输出清洗+校验,
  │                        LLM 失败或结果不可信时回退原话)
  │
  ├─(调用方显式点选智能体) ► multi_agent_execute ── 同一个(已消解的)问题并发下发给
  │                        点选的每个 A2A 专业智能体(hr/finance/analytics/procurement/askdata),
  │                        并发上限 Semaphore + 逐位超时 + 异常隔离, 一个智能体挂了只
  │                        降级它自己那一节; 程序化拼成"一节一个智能体"的 Markdown
  │                        (不再过 LLM); 本轮不走意图漏斗(MULTI_AGENT_ENABLED=false
  │                        即忽略点选, 回到下面的单意图分派)
  ▼
classify_intent ───────── 三层漏斗(规则快筛 → bge-m3 语义 → deepseek 兜底,
  │                        终端关键词保底)
  │
  ├── knowledge_qa ──► kb_retrieve ──► judge ──┬─ 有相关块 ──► kb_generate
  │                     ▲                └─ 空集且预算内 ─► kb_requery(换写法重检)
  │                     └────────────────┘ 重检后仍空 ──► kb_generate(明确拒答)
  ├── chitchat ──────► chitchat ──────── 直答(携带对话历史 + 消解提示)
  ├── tool_call ─────► tool_execute ──── MCP ReAct 工具调用(finance/hr/analytics/procurement,
  │                       │                角色×工具白名单过滤) + 进程内能力域:
  │                       │                web=联网检索/抓取, docgen=生成 Word/Excel/PPT/PDF/MD/图片
  │                       │                (纯进程内 tool, 无容器/端口)
  └── agent_delegate ► agent_delegate ── A2A 委派专业智能体(消解后的问题
                          │                作为当前请求, metadata 传身份; 委派 askdata 时
                          │                额外传上一轮确定的槽位供省略继承)
                          ▼
                    persist_memory ────── 脱敏后写会话记忆 + 一次 LLM 提取,
                          │                分桶沉淀为个人记忆(溢出摘要另存情节)
                          │                + 全链路审计(同一 trace_id)
                          ▼
                         END
```

要点:

- `resolve_time` 在意图识别**之前**固定执行,保证时间类回答以真实时钟为准;
- `rewrite_query` 前置于意图识别:分类器与**全部路由**共享消解后的
  独立问题,避免"那帮我查一下它的余额"因指代未消解而误分类/查错对象;
  首轮、无历史或自包含长问题(无代词标记)自动跳过,零额外开销;
- 知识库链路是 **retrieve → judge → (重检一次) → generate** 的回环:阈值裁到空集时
  先换写法重检(`RETRIEVAL_MAX_RETRIES`,默认 1 次),重检后仍空或命中全被 ACL 踢掉则**不进
  LLM** 直接给出拒答文案(不给参考来源),避免把噪声写成事实;
- 出口还有第二道**发布态门禁**(`docs_not_ready`):`status != ready` 或未注册元数据的
  文档视同无权,防"正在入库的半篇文档"被检索到;
- **多智能体并发委派靠显式点选触发**(`agent_targets`),系统不推断"该问谁": 复合问法的
  LLM 自动拆分已下线(想拆准要靠不断加提示词, 收益与成本不成比例); 一个委派是一整轮
  ReAct, 比一次检索贵得多, 所以并发上限默认只放 2, 逐位超时与异常隔离一个智能体;
  权限不够/域键非法/超出可点选上限都只降级那一节并明写原因, 不静默吞掉用户的点选;
- 身份与用户请求文本分离:工具调用经 System 消息、A2A 经协议级 metadata
  下发操作者身份,并显式区分"当前操作者"与"任务目标用户";
- 每个节点均写审计记录,同一 `trace_id` 串联全链路。

### 重排序与置信阈值(真 cross-encoder)

RRF 只融合排名不判相关性, 全链路**唯一**的相关性阈值落在 rerank 阶段
(`app/rag/reranker.py` -> `HybridRetriever.retrieve`)：候选正文在 `attach_texts`
主键回表后就位, 一次性批量送 TEI 容器的 `BAAI/bge-reranker-v2-m3` 序列分类头打分,
输出 sigmoid 后的 **0\~1 相关性**; 低于 `RETRIEVAL_SCORE_THRESHOLD`(默认 0.4) 的噪声
不进 Context Builder, 裁到空集即"未检索到相关文档", 由上层 judge 改写重检或明确拒答。

- 稠密/稀疏两通道与 RRF 均**不设阈值**(分数不可比, 只负责召回); TEI 不可用/超时
  (总超时 3s)则本层降级为 RRF 融合序且阈值不生效, `score_mode` 随分数标度一并上报审计。
- 阈值只作在**打分集**上: 单批上限 `MAX_CANDIDATES=32`(对齐 TEI 默认
  `--max-client-batch-size`), 超出部分不打分、保持 RRF 相对序附在尾部; 生产
  `RAG_TOP_K=8` 远未触顶, 仅评测拉高 top_k 时需知道这一段不过阈值。
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
  `documents` 表为事实来源(`app/security/acl.py`);入库与权限变更
  (`PUT /api/docs/{doc}/acl`)都在**同一个 PG 事务**里改完元数据行 + 两张块表 +
  `outbox_events`(`app/docs/service.py::update_document_acl`)。分两个事务写就会留下
  「元数据已改 private、块表还是 public」的越权窗口, 而不是单纯的数据不一致。
  **ACL 永远以 PG/ES 标量列做前置裁剪, MongoDB 只按** **`_id`** **取文本, 不承载权限语义**;
  正文与 `extra`(JSONB) 只放展示字段, 权限字段禁止入 JSONB。
- **可见性策略**:`public`(全员) / `dept`(指定部门) / `role`(指定角色)
  / `private`(仅上传者);管理员角色全量可见。未知 visibility 一律 default-deny。
  `allowed_roles` 以逗号包裹形式存储(`",hr,admin,"`), PG 侧 LIKE 匹配前转义
  `%`/`_`/`\`, ES 侧则拆成 keyword 数组做精确 term。
- **前置裁剪**:稠密通道用 SQL 谓词(`app.rag.vectorstore.build_sql_filter`, 作用于
  `doc_chunks`), 与列白名单 `NARROW_COLUMNS`(不含 `chunk_text`/`embedding`) 一起作用在
  `ORDER BY embedding <=> ? LIMIT k` 之前, 在 ANN 检索阶段即排除无权文档且不拉宽行; 命中的窄行
  在 rerank 之前按 `chunk_id` 主键批量回表补正文。稀疏通道(Elasticsearch BM25)用等价的
  bool filter(`app.rag.bm25._acl_filter`), 两通道逐条语义严格一致。
- **最终授权**:`kb_retrieve` 在父块组装后、拼接 Context 前再逐条复核一次(判定口径单一事实源
  `app.security.acl.is_allowed`, 与 SQL 谓词/ES filter 逐条对应),
  拦截组装/脏数据可能引入的越权块,剔除项写审计(`acl_final_check_dropped`);越权与"知识库没有"
  用同一拒答文案,不泄漏文档存在性。

### 文档入库流水线(异步任务 + 事件投影)

文档入库是整系统里最重的一条写路径(OCR/解析/LLM tags/切块/embedding/发布/建索引/建图),
因此它**不在 HTTP 请求里跑**。单一事实源: `app/docs/pipeline.py`(阶段实现) +
`app/workers/ingest_worker.py`(进程入口) + `doc_uploads`/`ingest_tasks`/`outbox_events` 三张表。

```
POST /api/docs/upload   202  流式落盘 + 算 sha256 → doc_uploads 行 + outbox(parse.requested)
        │                     同事务提交, 立即返回 upload_id
        ├─► ingest-worker ──► 解析/OCR → Mongo parse_artifacts(偏移表 + raw 一份)
        │                     + LLM tags → uploads.status=parsed
GET  /api/docs/uploads/{id}   前端轮询: 预览 / 建议标签 / 告警   ← 预览与入库用同一份产物
POST /api/docs/ingest   202  同步校参(ACL 非法即 400) + 建 ingest_tasks 行
        │                     + 新文档置 ingesting + outbox(ingest.requested) 同事务
        ├─► ingest-worker ──► parsing → embedding → publishing → indexing → ready
        │       publishing = 单事务: documents 转 ready + tags + 父子块 upsert
        │                    + 不再引用的行标 deprecated_at + outbox
        │       indexing   = ES 单文档增量写 + 删被标陈旧的块 + bump docgen + 建图
GET  /api/docs/tasks/{id}     前端轮询: stage / attempt / error / chunk_count / warnings
```

关键口径(改这条链路前必读):

- **PG 是事实源, Redis Streams 只是低延迟 handoff**。`outbox_events` 与业务写同事务提交,
  因此不会「错投/漏投」; worker 的 `db_poll` 循环会直接按任务表认领, 所以 Redis 挂了
  入库照跑(时延从秒退化为轮询间隔), 事件恢复后多触发一次认领也会因租约已被持有而跳过。
- **认领与并发靠租约**:`UPDATE ... WHERE lease 空或过期 RETURNING` 一条语句完成「判定 +
  抢占」, 拿不到就跳过; `ingest_tasks` 上有一条部分唯一索引
  `ux_ingest_tasks_active_doc`, 同一文档的第二个进行中任务插不进来。
- **幂等有三重**:`task_id = sha1(doc_key|upload_id|content_sha256)`(连点两次得同一行)、
  同字节同 `parser_version` 复用已解析产物(不重复 OCR)、阶段全部可重放
  (upsert 幂等 / content_hash 差分 embed / ES 按 id 写删)。
- **失败闭环**:`stage + attempt + lease_expires_at + heartbeat_at + error` 让「worker 崩在
  半途」变成可回收状态: 调度器 `reap_stalled_tasks` 清租约让任务从**当前阶段**续跑,
  超过 `ingest_task_max_attempts` 才判 `failed`(只把从没成功过的文档置 `failed`,
  在架文档重入库失败不动它的 `ready`——否则一篇在用的文档会因为重试而整篇检不到)。
- **发布不物删旧块**:主链路只 `UPDATE deprecated_at`, 物理删除由 `gc_deprecated_rows`
  按 `ingest_stale_grace_minutes` 宽限期分批做(多副本靠 `pg_try_advisory_lock` 单飞)。
  旧写法在单事务里先删子块再删父块, 大文档会长时间持锁并给并发查询留空窗。检索侧
  每条读路径都带 `deprecated_at IS NULL`, 所以新旧交接在同一瞬间完成, 既无空窗也无重复。
- **ES 常态是单文档增量写**(`index_doc_chunks` / `delete_chunk_ids`);全量重建只作为
  运维/修复手段(`python -m scripts.rebuild_bm25`, 网关启动也只在索引为空时灌一次)。
- **Retrieval Cache 按文档粒度失效**:`bump_doc_gen([doc_key])` 只 O(文档数) 次 INCR;
  缓存 payload 里存了本次命中文档的版本快照, 读时一次 MGET 比对, 不符即当未命中并自删。
  入库热路径不再 `invalidate_all()`(那会把全站热查询一起打空)。
- **worker 不做 DDL**:建表唯一归属在网关 lifespan 的 `init_schema()`;worker 只轮询
  三张表就绪。两个进程同时跑 `create_all` + ALTER 会出现「一个在补列、一个在读该列」。
- **原件与产物落共享卷**:上传目录按**相对 `upload_dir` 的路径**存库(绝对路径是容器内
  `/data/uploads/...`, 宿主脚本读到就指向不存在的位置);`ingest-worker` 与网关挂同一个
  `../data:/data`。到期由 `cleanup_expired_uploads` 按 `upload_retention_hours` 回收,
  孤儿正文/产物由 `prune_stale_bodies` 按 PG 现行引用回收。

已知取舍(本次有意不做, 需要时再评估):对象存储/病毒扫描/分片上传协议(当前单实例 +
共享卷, 多副本部署时才需要真对象存储)、`status/index_status/kg_status` 三态拆分(现在
由 `ingest_tasks.stage` 统一表达进度, 文档表只留发布态)、`embedding_spec_hash` 复合指纹
(仍按 `content_hash` 差分; 换 embedding 模型时靠 `EMBEDDING_DIM`/全量重入库覆盖)、
上传文件魔数校验。

### 数据洞察智能体的六层数据访问治理

Analyst_Agent 能跨 HR/财务/采购查全员数据, 也能受治理地改数据。"只靠提示词让模型小心"
不成立, 因此这里是一层往一层下堆叠的硬约束(单一事实源: `app/db/rls.py` /
`app/db/ast_guard.py` / `app/db/dataops.py`)。

| 层         | 干什么                                                                                                                                                                                                                                                                    | 代码落点                                                                                                         |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| 0 凭证     | agent 容器不再持有任何数据库凭据(已验证: `hr-agent`/`analyst-agent`/`finance-agent`/`contract-agent`/`askdata-agent` 取口令直接报错); "姓名->工号"解析从进程内直连库搬到 `hr-mcp` 的 `lookup_employee_by_name`; analytics/askdata 取数与写执行 `SET ROLE` 到 NOLOGIN 角色 | `docker-compose.yml` 五个 agent 服务、`app/mcp_servers/hr_server.py`、`app/db/sync.py::_attach_role_events`      |
| 1 隔离     | 8 张业务表带 `tenant_id`/`dept_id`; `ENABLE + FORCE ROW LEVEL SECURITY`(属主也受约束); GUC 缺失 = 一行也看不见(默认拒)                                                                                                                                                    | `app/db/rls.py`、`app/db/scope.py`(回填 + 作用域解析), 由 `init_schema()` 尾部幂等执行                           |
| 2 写形状   | 模型不写 SQL, 只交强类型 JSON DSL(`action`/`entity`/`filters`/`sets`/`reason`); 实体与字段逐个查白名单, 作用域谓词由服务端注入, 值全走绑参                                                                                                                                | `app/db/datadsl.py`、`app/db/policy.py`、`app/db/dataops.py::compile_plan`                                       |
| 3 静态校验 | sqlglot AST 校验(语句数/类型/表引用/域谓词形状/恒真 WHERE/函数黑名单/十六进制与 `CHR()` 拼接/注释), 并**由 AST 重生 SQL** 送库; 外加 EXPLAIN 预估扫描行数阈值                                                                                                             | `app/db/ast_guard.py`(仅 analytics 域; 其余三域仍是 `app/db/sql_guard.py` 的正则黑名单)                          |
| 4 误操作   | 预演 COUNT 后分四档: 0 行需复核 / 1~50 发起人二次确认 / 51~500 人工审批(审批人≠发起人) / >500 直接拒; DELETE 一律改写成软删; 变更前镜像支持回滚; 单事务 + 语句超时 + 专用小池; 高危表(员工/预算/供应商/审计)永无写权                                                      | `app/db/dataops.py`、`app/db/policy.py::FORBIDDEN_ENTITIES`、`app/dataops/router.py` + `DataOpsView.vue`(审批台) |
| 5 注入分治 | A 类参数绑定根治; B 类靠权限+AST+审批; C 类间接提示注入: 读回来的数据带 `untrusted_data`+声明(spotlighting), 敏感列出口 DLP 打码, **本轮读过业务数据就不允许发起写计划**(信息流控制), 写意图词表核对计划偏移                                                              | `app/security/spotlight.py`、`app/security/masking.py`、`app/agents/analyst_agent/executor.py`                   |
| 6 审计     | 每条 SQL/写计划落 `sql_audit_records`(原文/最终 SQL/决策/审批人/行数/镜像/成本), 与 JSONL 双写; 角色无 UPDATE/DELETE + RULE 拦改; 异常模式实时告警(频繁写计划/拒绝激增/跨域谓词尝试)                                                                                      | `app/db/dataops.py::record_audit`/`detect_anomalies`、`app/db/rls.py::ensure_audit_immutability`                 |

三个容易误读的点:

- **隔离并不是"代码里加了 WHERE"**: 只读路径靠 RLS 兜底(`SET ROLE` 后策略才真生效,
  所以 `FORCE` + `NOBYPASSRLS` + `NOLOGIN` 三者缺一不可)。`RLS_ENABLED=false` 会退回
  "只剩软控制"的状态, 此时固定口径指标/报告对部门级角色直接拒执行(否则就交出全员数字)。
- **manager 被收紧了**: 改造前经理在 analytics 域"全量可见", 现在只读全量工具集,
  且数据范围默认限本部门; 能改数据的角色由 `DATAOPS_WRITABLE_ROLES` 控制(默认 finance/admin)。
- **写不是一步完成的**: A2A 卡片 `streaming=False`, ReAct 循环里无法中途向用户要确认,
  所以 `plan_data_op` 只生成 `op_id` + 人话回显, 执行发生在下一轮发起人
  `confirm_data_op` 或审批台批准; 这条两段式同时就是上面的信息流控制。

自检: 离线纯函数 `python -m scripts.test_ast_guard_offline` 与
`python -m scripts.test_dataops_dsl_offline`; 端到端隔离(含 RLS/写作用域/AST 绕过面)
必须在容器里跑: `docker compose -f docker/docker-compose.yml exec assistant python -m scripts.test_scope_isolation`;
RLS 当前结论可看一眼: `python -m scripts.init_db --rls-status`。

### 智能问数：语义层 + NL2DSL 的确定性状态机

AskData_Agent(:9007) + askdata-mcp(:8007) 的目标不是"能回答更多问题", 而是**每个数都说得出
怎么算的**。企业问数里错的数比没数危险得多(错的数会被写进汇报), 所以主干不自由:

```
PARSE(槽位) -> LINK(三路召回) -> CLARIFY? -> PLAN(DSL) -> VALIDATE(三级) ->
ROUTE(指标API -> DSL编译器 -> 沙箱SQL) -> VERIFY(结果校验) -> [REPAIR ≤2 轮] ->
RENDER(规则选型 + 叙述) -> FEEDBACK(留痕 + 归因路由)
```

| 阶段   | 硬约束                                                                                                                                                                                                                                         | 代码落点                                                                                      |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 语义层 | 指标/维度/枚举值/别名/兼容规则/行列策略是**库里的治理对象**(52 指标 + 33 维度 + 83 别名 + 67 枚举值 + 276 条兼容规则), 真值是仓里 `semantic-defs/*.yaml`(可 review/可回滚), 口径表达式是受控列级片段; 与周报重叠的口径共用 `reports.py` 常量   | `semantic-defs/`、`app/db/models.py`(sem\_\*)、`app/semantic/{defs,catalog,seed,registry}.py` |
| 链接   | 同义词精确 + pgvector 相似 + 枚举值(含黑话别名与热度)反查三路融合打分, 槽位置信度作为先验乘进分数, **不够就澄清不猜**                                                                                                                          | `app/semantic/link.py`                                                                        |
| DSL    | 模型只交强类型 JSON: 无标识符自由(列名出自登记表)、无值拼接自由(全走绑参)、无作用域自由、**无算术自由**; 生成时带 golden 相似问法的 dynamic few-shot                                                                                           | `app/semantic/dsl.py` + `similar_cases` 工具                                                  |
| 校验   | 独立模块不信模型自检: 语法→语义(兼容性/时间/枚举)→权限与成本(角色白名单、列级策略、行数与扫描量) 三级, 带错误码回炉重写 ≤2 轮                                                                                                                  | `app/semantic/validate.py`                                                                    |
| 编译   | DSL→SQL 全模板: 裸列自动限定表别名、作用域谓词 + **行策略**注入、产物再过 `ast_guard`, 同环比走**单扫描条件聚合**(解不出口径形状时退回双窗口 CTE)                                                                                              | `app/semantic/compiler.py`                                                                    |
| 取数   | 只读角色 + 事务作用域 + EXPLAIN 扫描量上限 + 行数上限; 出口 DLP 打码与 spotlighting; 结果缓存 key = normalize(DSL)+身份+semver; 引擎连续错误熔断后短路下一级                                                                                   | `app/mcp_servers/askdata_server.py`、`app/semantic/{resultcache,breaker}.py`                  |
| 呈现   | 图型由结果 shape 规则定(时序→line/同环比→分组柱/低基数→pie/二维→按第二维分系列), LLM 只写叙述                                                                                                                                                  | `app/agents/askdata_agent/{graph,prompts}.py`                                                 |
| 闭环   | 全链路进 `askdata_query_log`(含 semver/token_cost/缓存命中/被澄清与被拒的轮次), 看板算一次通过率/澄清率/**误澄清率**/降级率/FALLBACK 率/P95/点踩率; 五类归因以 `edit_dsl` 的字段 diff 为直证, 只产出**未生效的草案**; 答不出来时开**人工工单** | `app/askdata/{store,attribution,router}.py` + `/api/askdata/*`                                |
| 可观测 | OTel span(每工具一条 + 整轮一条, 属性 conf/召回分/route/重试/tokens/semver) + Prometheus `/metrics` + 四条告警线; 与 Langfuse/audit.jsonl 并存                                                                                                 | `app/observability/{otel,metrics,alerts}.py`                                                  |

四个设计取舍值得记在这:

- **状态机而不是 ReAct**: 主干每步输入输出形状固定, 可单测可回放可归因; 自由 ReAct 只留在
  结果校验失败后的有界自修复(默认 ≤2 轮, 工具集只有只读四件)。
- **澄清用选项不用开放反问**: 开放式反问会得到另一句同样含糊的话; 选项带语义层 id,
  前端拼一条带精确引用的确认消息回去(`问数确认：…`, 前缀保证路由回 askdata)。点选了
  最高分候选的那一轮会被记成 `clarify_pick` 事件 —— 它是**误澄清率**的分子, 否则只算得出"问了几个"。
- **多轮继承存"上一轮定稿"而不是会话摘要**: `app/assistant/slot_store.py` 存上一轮的槽位 +
  DSL + 最高分口径(Redis + 进程内降级), 继承是 dict 合并 + 指代消解喂 DSL 摘要, 只继承时间/指标
  不继承维度与过滤 —— 后者会把上一轮的筛选悄悄带进这一次的数, 是最难发现的错。
- **结果缓存的 key 必须带身份**: 方案给的三段式(`normalize(dsl)+role+semver`)在本仓不够 —— 同为
  manager 的两个人可见行集不同, 按它做键就是一次跨部门泄露且看板上看不出来, 所以工号与作用域也在 key 里。
- **时间以原话为准, 不信模型换算**: 相对区间只认词表(`timesem` 含`next_*` 前向窗口: 未来 30 天
  与过去 30 天是两个完全相反的答案, 不能拿后者顶前者); 提示词里明示"今天是几号"且要求
  相对时间只填区间词; DSL 里只要出现"区间词 + 硬日期"或"只有硬日期但原话说了本月",
  都在 `validate` 节点(三条路径的汇合点)对账后**重新按原话算**。实测过的错形是
  用户问"本月"而模型交回 9 月整月(当时已 10 月), 结果看起来完全自洽 —— 这类错不会报错, 只会静默给错数。

语义层真值与版本(YAML → PG 单向):

```bash
python -m scripts.export_semantic_defs --from-seed     # 第一次把内置集落成 YAML(可 --from-db 回写人工修订)
python -m scripts.sync_semantic_defs --check           # CI: 真值与库不一致就退码 1, 不写库
python -m scripts.sync_semantic_defs --actor <谁>       # 同步: 内容有变才 version+1 并写 sem_change_log
python -m scripts.seed_semantic_layer                   # 别名/指标向量回填
```

Trace 与留痕里都带 `semver`(定义集内容指纹), 所以"一个月前的 badcase 当时用的是哪版口径"
可以反查(`sem_change_log`)并回放。临时直接在库里改口径是可以的, 但**必须回写 YAML**,
否则下一次同步会按 YAML 覆盖它 —— 口径的最终形态只允许有一个来源。

语义缓存与两层缓存的区别(别弄混它们的责任):

| 层           | 键                                                                | 复用什么      | 不能省下什么                    |
| ------------ | ----------------------------------------------------------------- | ------------- | ------------------------------- |
| 语义缓存(V3) | 问法向量相似度 ≥0.95 **且 semver 同版 且已人工批准(`cacheable`)** | 查询意图(DSL) | 校验、编译、RLS、审计、结果缓存 |
| 结果缓存     | normalize(DSL)+route+工号+角色+作用域+semver                      | 结果行        | 口径与权限判定(它们都在键里)    |

两者都不省校验: 语义缓存命中时状态机仍然跑 link/validate/route/verify, 所以指标被停用、
口径改了版、权限不够这些情况仍旧会被拦下而不是"静默复用了一个旧答案"。

**为什么合成 golden 默认不可复用**(`askdata_golden.cacheable` 默认 false): 一是批量生成的
问法没经过人逐条核对, 不该直接服务真实用户; 二是 agent 模式的评测题就来自 golden,
全部可缓存等于让模型拄标准答案 —— 本轮实踩过一次: 留痕里 165/277 轮带"相似度 1.000 复用"
注记, 于是 EX 从 87.5% 直接刷到 100%, 而 NL→DSL 能力一点没变。要打开只能逐条批准:
`POST /api/askdata/golden/{case_id}/cacheable?cacheable=true`(只给治理角色)。

样本回流与语义层修订(都是"机器提议 + 人批准"):

```bash
python -m scripts.embed_askdata_golden            # golden 问法向量回填(语义缓存基底)
python -m scripts.askdata_semantic_advisor --days 30            # badcase 簇 -> YAML 修订建议(只出文件)
python -m scripts.askdata_semantic_advisor --days 30 --apply    # 只追加 add_term/add_dim_value, 且 status=draft
GET  /api/askdata/samples                                       # 看 SFT/DPO 队列与两道门禁统计
POST /api/askdata/samples/{id}?status=approved                   # 人工批准
python -m scripts.export_askdata_samples --dry-run              # 只导 approved 且过双门禁的
```

为什么样本要过两道门: 错的样本比不训更危险。`chosen` 必须**真能过校验器并编成 SQL**,
并且这一轮得用户点过赞或亲自改写过 —— 否则它就是模型自己给自己的考卷打分。

自检与评测:

- 离线纯函数(不碰库不碰 LLM): `python -m scripts.test_askdata_offline`
  (DSL/三级校验错误码/编译产物形状/口径一致性/澄清门/载荷剥离/真值往返/semver 稳定性/
  行列策略下推/熔断与缓存 key/图表形状/DSL-EM 与 slot-F1 判据/告警线/注册一致性);
- 在线全链路(打容器网关): `python -m scripts.test_askdata`;
- golden 评测集: `python -m scripts.seed_askdata_golden --refresh`(按 intent × 难度分层展开,
  公开基准噪声大, 只认自建集);跑完接着跑 `embed_askdata_golden` 回填问法向量;
- 回归: `python -m scripts.askdata_eval`(DSL 编译回归, 无 LLM) 与
  `python -m scripts.askdata_eval --mode agent`(端到端), 加 `--gate` 就是 CI 门禁
  (EX 比上一条同模式 run 掉超 `ASKDATA_EVAL_GATE_MAX_DROP` 个点则退码 3);
  报告进 `reports/askdata_eval_*`, 结论(EX/DSL-EM/slot-F1/误澄清率/门禁)进 `askdata_eval_run`。

治理面入口(前端页): `web-ui/src/views/AskDataOpsView.vue`(导航"问数治理", `/askdata`)。五个页签分别接
`/api/askdata/{stats,alerts,tickets,samples,badcases,golden,semantic}`: 运行指标与分母说明、四条告警线 +评测 CI 门禁、人工工单(领取/结办/驳回)、SFT/DPO 样本队列(批准/驳回, 两道门禁列直接可见)、
评测集的**语义缓存复用批准**(默认全不可复用, 逐条人批)与口径版本/变更记录。入口只对
`ANALYTICS_ALL_DEPT_ROLES` 里的角色可见 —— 这只是体验, 每个接口都服务端重查角色。

可观测栈是**可选**的(默认不启): `docker compose --profile observe up -d otel-lgtm` 起
OTel Collector + Prometheus + Loki + Grafana 四合一(宿主 18300/18686), 再把 `docker/.env` 的
`ASKDATA_OTEL_ENABLED=true` 与 `OTEL_ENDPOINT=http://otel-lgtm:4318/v1/traces` 置上;
指标端点 `GET /metrics` 始终由网关提供(`METRICS_ENABLED`, 配 `docker/secrets/metrics_token.txt`
后可加口令)。

### 三层缓存与降级口径

三类缓存统一落 Redis(`app/cache/`), 全部按"能降级就降级"设计: Redis 关闭/连不上
(`get_redis()` 返回 None)就退回直连真实调用, 行为等同于本功能上线前, 不报错不阻断。

| 缓存                                  | key 组成                                         | 默认 TTL         | 接入门槛(默认拒)                                                                                                                                                                                                              |
| ------------------------------------- | ------------------------------------------------ | ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Prompt Cache(`prompt_cache.py`)       | `model\|temperature\|sha256(prompt)`             | 300s             | 只给"同 prompt → 同结果"的纯函数式调用: 改写/意图 LLM 兜底/闲聊直答;**严禁用于知识库生成**(输出携带文档级 ACL 与实时检索结果)                                                                                                 |
| Retrieval Cache(`retrieval_cache.py`) | `query\|top_k\|top_n\|ACL签名(user\|dept\|role)` | 300s             | 命中即跳过整条检索(含前置权限裁剪), 故 key 必须带身份签名; 失效按**文档粒度**做: payload 存命中文档的 docgen 版本快照, 读时一次 MGET 比对, 不符即未命中并自删(`bump_doc_gen`); `invalidate_all()`(SCAN 而非 KEYS)只留运维路径 |
| Tool Cache(`tool_cache.py`)           | `server\|tool\|args(sort_keys)\|role`            | 30s(web 域 300s) | 工具名需命中只读前缀白名单 `query_/list_/get_/check_/lookup_/search_`;`generate_*`/`create_*`/`submit_*` 天然不命中;A2A 委派整体不接入                                                                                        |

两个刻意保留的"不优化":流式闲聊不读 Prompt Cache(命中的重放没有思考过程, 收益小于
体验损失);多智能体分节合并不再过一次 LLM(各节已是智能体产出的事实, 再过一次只会
引入改写与编造风险)。

### 个人级记忆的双时态(会随时间波动的属性)

体重/身高/年龄/部门/职位/职级/汇报对象/所在地/入职时间这类值不是"身份", 而是**随时间
变化的观测序列**。拿写入顺序当真相会出事故: 用户先说"我现在 70kg", 后一句"2015 年秋
我才 64kg"就把当前态改成了一个十年前的值。现行口径与业界一致(健康数据建模的 FHIR
`Observation`: 观测只追加, 当前值按 effective 时间派生; 时序知识图谱 Graphiti/Zep 的
`valid_at`/`invalid_at` 双时态与 fact invalidation; OpenAI 个性化记忆 cookbook 的
"冲突按日期取最新"), 实现集中在 `app/memory/temporal.py`:

| 属性类别                                         | 存储与合并语义                                                                                                                                       |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| 波动类(`_MEASURE_KEYS`)                          | 值存成 `(值, valid_at 生效时间, recorded_at 记录时间)` 观测序列(`user_profiles.attribute_history`), **当前值按生效时间派生**; 在讲过去的陈述只进历史 |
| 身份修正类(`_CORRECTION_KEYS`: 姓名/工号/生日等) | 覆盖即修正, 不留历史                                                                                                                                 |
| 多值类(技能/负责事务/家庭等)                     | 并集去重, 只增不删(删旧值留给用户在"我的记忆"页自服务)                                                                                               |
| 记忆条目(情节/知识)                              | 语义查重命中且 `occurred_at` 相差在 `memory_observation_window_days` 内才合并, 否则视为不同时刻观测各存一条; 合并时时间只往前走                      |
| 个人图谱(`:REL` 边)                              | 带 `valid_at`/`invalid_at`/`as_of`; 单值关系被新事实取代时旧边只关窗不删, 补说的旧事写入即失效                                                       |

三个容易踩的点:

- **两轴分开**才拦得住事故: 用户明说的时间进 `valid_at`(现实轴), 何时听到的进
  `recorded_at`(录入轴); 只在 `explicit=True` 时画像摘要才渲染"（自 2026-09）", 历史值不进
  prompt(只给前端的"我的记忆"页看), 不然 `profile_max_chars` 预算会被吃掉。
- **近期起始日期不算历史**: "从上个月起我改汇报给张总" 描述的是**持续到当下**的变更,
  所以在 `profile_current_grace_days`(默认 90 天)内的明说时间仍算当前态、能顶掉旧值;
  超出宽限期才归入历史。画像与图谱共用同一个宽限期, 否则两个召回通道会给模型矛盾答案。
- **情节侧的 `occurred_at` 仍只收近三年**(`extraction._parse_date`): 情节有
  `episodic_window_days` 时间窗过滤, 模型猜错年份会让整条记忆被误杀; 画像的 `valid_at`
  只参与排序(猜错最多沉进历史), 所以放到 1900 年(`temporal.MIN_VALID_AT_YEAR`)。

### 什么才配被记住: 情节与个人图谱的写入口径

"本轮让助手干的活"不是记忆: 用户说"把男子100米历史前10做成 Excel 下载", 助手生成
了文件 —— 这件事已经躺在会话记录里, 跨会话记住没有价值。旧口径下这类轮次会往情节桶
灌进"生成百米历史前十表格"一条, 同时往图上挂"男子100米历史前10好成绩.xlsx"、
`docgen-20260930-234924` 这种产物节点。现行口径两条:

| 桶           | 只装                            | 拦掉                                                           | 口径位置                                                            |
| ------------ | ------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------- |
| 情节 episode | 用户现实/工作里真实发生过的经历 | 本轮对话产物("用户要求…/助手生成…/产物编号/下载链接")          | `MEMORY_EXTRACTION_PROMPT` + `taxonomy.is_conversation_product`     |
| 个人图谱     | 以用户为中心的关系网            | 提到即建点、第三方世界知识、文件名/报告标题/单号、词表外关系词 | `app/memory/graph_vocab.py`(纯函数) → `graph_store.upsert_entities` |

图侧四条硬口径(与 `app/kg/vocab.py` 对称, 枚举只定义一份, 提示词、落库、清理脚本、
前端标签都仍从这里取): **实体类型白名单**(person/department/organization/system/position/place,
明确标成 topic·document 的不入库) · **关系词受控**(表外宁可不写, 不做"归到兜底词") ·
**产物名不进图** · **边必须锚定在用户身上**(至少一端是"我"或该用户图里已有的节点,
且只为保留的边建点 —— 孤立节点是旧版污染主因)。姓名/工号在写入前折叠成"我"这个
中心节点, 避免同一个人裂成两点。存量脏数据用 `python -m scripts.clean_personal_graph`
(默认 dry-run, 同一份口径)清理; 离线自检 `python -m scripts.test_graph_vocab_offline`。

## 目录结构

```
mxi/
├── app/
│   ├── config.py                 # 全局配置(pydantic-settings)
│   ├── schemas.py                # 共享数据模型(意图/角色/知识块)
│   ├── main.py                   # FastAPI 网关入口(有 web/dist 才托管 SPA, 否则提示走 vite dev)
│   ├── assistant/                # ★ Assistant 调度核心
│   │   ├── graph.py              #   LangGraph 编排:消解→三层意图→分层路由(可点选多智能体并发)→记忆
│   │   ├── intent.py             #   意图三层漏斗(规则 → bge-m3 语义 → LLM → 关键词保底)
│   │   ├── memory.py             #   短期窗口 + LLM 摘要长期记忆
│   │   ├── stream.py             #   SSE 事件缓冲区(RunBuffer/StreamHub, 断点重放)
│   │   ├── mcp_client.py         #   MCP Client(langchain-mcp-adapters)
│   │   ├── a2a_client.py         #   A2A Client(Agent Card 发现/message.send)
│   │   ├── prompts.py            #   四类路由/改写/记忆提取 Prompt
│   │   └── router.py             #   /api/chat 与 /api/chat/stream 统一入口(含 /api/agents 点选清单)
│   ├── memory/                   # ★ 个人级记忆层(按 user_id 隔离)
│   │   ├── taxonomy.py           #   分桶语义单一事实源(kind/注入方式/标签)
│   │   ├── temporal.py           #   双时态口径单一事实源(生效轴/录入轴/历史判定)
│   │   ├── personal.py           #   编排:读路径并行召回, 写路径分桶落盘
│   │   ├── profile_store.py      #   画像(user_profiles 表, 当前值派生 + 有界历史)
│   │   ├── vector_store.py       #   情节/知识(向量召回)与偏好/习惯(标量直读)的 pgvector 长表
│   │   ├── graph_store.py        #   个人图谱(Neo4j, :MemoryUser 锚点, REL 边带生效/失效时间)
│   │   ├── graph_vocab.py        #   图谱写入口径(实体类型/关系词表/产物拦截/用户锚定)
│   │   ├── extraction.py         #   一次 LLM 调用产出全部桶(画像/关系带生效时间)
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
│   ├── docs/                     # ★ 文档解析与入库流水线
│   │   ├── normalize.py          #   normalize_text/offset_slice/content_hash(offset 基准)
│   │   ├── parsers.py            #   各模态解析(txt/md/pdf/docx/pptx/xlsx/字幕/图片 OCR)
│   │   ├── artifacts.py          #   解析产物门面(一次解析, 预览与入库共用同一份)
│   │   ├── pipeline.py           #   入库四阶段实现 + outbox + 回收/GC/过期清理
│   │   ├── service.py            #   提交(202)/状态查询/ACL/删除(同事务+事件)/列表
│   │   └── router.py             #   /api/docs 路由(upload/ingest 异步 + uploads/tasks 轮询)
│   ├── workers/                  # ★ 后台 worker 进程(与网关同镜像, 不同 command)
│   │   └── ingest_worker.py      #   Streams 消费 + PG 认领自愈 + outbox relay + 调度器
│   ├── mcp_servers/              # ★ MCP 工具层(业务系统封装)
│   │   ├── hr_server.py          #   HR 工单系统(:8001/mcp)
│   │   ├── finance_server.py     #   财务报销系统(:8002/mcp)
│   │   ├── analytics_server.py   #   数据洞察(:8005/mcp, Text2SQL/图表/周报)
│   │   ├── procurement_server.py #   采购与合同初审(:8006/mcp)
│   │   └── askdata_server.py     #   智能问数(:8007/mcp, 语义层/指标API/DSL编译/沙箱取数)
│   ├── analytics/                # ★ 数据洞察支撑(不引 matplotlib/cairosvg)
│   │   ├── charts.py             #   纯 Python SVG 图表(bar/line/pie/heatmap) + Pillow PNG(供 office 嵌图)
│   │   ├── reports.py            #   固定口径指标 SQL + Markdown 报告组装
│   │   └── store.py              #   产物落 data/reports + 台账 + 相对 URL 寻址
│   ├── docgen/                   # ★ 文档生成(文字+图片 -> 可下载 office/PDF/MD 文件)
│   │   ├── genstore.py           #   生成物令牌/gen/<token>/ 落盘/保留期清扫 + spec 解析
│   │   ├── images.py             #   图片解析层: 本地文件/URL(过 SSRF) -> 归一化 PNG
│   │   ├── docx_builder.py       #   python-docx 构建器(文字/表格/嵌图, 同步)
│   │   ├── xlsx_builder.py       #   openpyxl 构建器
│   │   ├── pptx_builder.py       #   python-pptx 构建器
│   │   ├── pdf_builder.py        #   reportlab 构建器(内置 CID 中文字体 STSong-Light)
│   │   └── md_builder.py         #   Markdown 构建器(零依赖)
│   ├── tools/                    # ★ 进程内工具能力域(纯 tool 形态, 无容器/端口)
│   │   ├── web.py                #   search_web(ddgs/tavily/serper, 失败降级) + fetch_url
│   │   ├── docgen.py             #   generate_docx/xlsx/pptx/pdf/md/image(回下载链接)
│   │   └── _http.py              #   共享 httpx 连接池(follow_redirects=False 逐跳校验)
│   ├── files/                    # ★ 生成物下载路由 /api/files/{token}/{file_name}
│   │   └── router.py             #   四层防护: 令牌形状/文件名安全集/子树断言/扩展名→MIME 白名单
│   ├── procurement/              # ★ 采购/合同确定性规则引擎
│   │   └── rules.py              #   必备条款/高风险表述/金额分级/预算余额
│   ├── semantic/                 # ★ 问数语义层(指标中台元数据)
│   │   ├── catalog.py            #   指标/维度/别名的内存快照形状(prompt 与校验共用)
│   │   ├── seed.py               #   内置 Top 指标/维度/别名集(灌库源 + 无库时的兜底)
│   │   ├── registry.py           #   读 sem_* 三表 + TTL 缓存 + 幂等灌入
│   │   ├── dsl.py                #   受约束查询 DSL(强类型, 拒绝作用域列/表达式)
│   │   ├── timesem.py            #   时间语义: 区间词 -> 左闭右开区间与对比期(MTD 环比对齐同期)
│   │   ├── validate.py           #   三级校验器(语法→语义→权限), 带错误码
│   │   ├── link.py               #   三路召回链接(词典/向量/枚举反查, 枚举带作用域)
│   │   └── compiler.py           #   DSL→SQL 编译器(模板化 + 作用域注入 + AST 自校验)
│   ├── askdata/                  # ★ 问数治理面(留痕/看板/归因/评测集)
│   │   ├── store.py              #   同步 DAO: 问数留痕 + stats 聚合 + golden + 评测结论
│   │   ├── attribution.py        #   五类归因与草案路由(只产出未生效的草案)
│   │   └── router.py             #   /api/askdata/{stats,queries,feedback,badcases,golden,semantic}
│   ├── agents/                   # ★ A2A 专业智能体
│   │   ├── finance_agent/        #   agent_card / executor / server(:9002)
│   │   ├── hr_agent/             #   agent_card / executor / server(:9001)
│   │   ├── analyst_agent/        #   数据洞察 agent_card/executor/server(:9005)
│   │   ├── contract_agent/       #   采购合同 agent_card/executor/server(:9006)
│   │   └── askdata_agent/        #   智能问数(:9007): graph 确定性状态机 + prompts + executor
│   └── security/                 # ★ 安全治理
│       ├── auth.py               #   角色→工具/Agent 白名单
│       ├── url_guard.py          #   SSRF 护栏(字面名拒/白名单/解析即校验 is_global)
│       ├── acl.py                #   文档级 ACL(Principal→谓词/SQL/ES filter)
│       ├── audit.py              #   全链路审计 JSONL(trace_id 串联)
│       └── masking.py            #   身份证/银行卡/手机号/金额脱敏
├── scripts/
│   ├── dev_services.py           # docker 全栈一键起 + 对接自检 + env-check 防覆盖(含密钥/串味守护)
│   ├── dev.ps1                   # 开发启动器: docker 全栈(含网关容器) + vite dev(-Stop 可停)
│   ├── package.py                # 一键打包前后端为部署目录(内含两道密钥闸门)
│   ├── ingest_knowledge.py       # 知识库构建脚本
│   ├── init_db.py                # PostgreSQL + pgvector 建表/自检
│   ├── migrate_doc_stores.py     # 存量迁移与校验: 正文入 Mongo + 父子拆双表
│   ├── migrate_kg_edges.py      #   文档图谱边迁移: 补溯源 docs + 关系词归一 + 清孤儿实体
│   ├── bench_doc_stores.py       # 父子双表规模基准(只写独立评测库)
│   ├── test_sse_resume.py        # SSE 断点续流端到端用例(可走 vite 代理 MXI_BASE)
│   ├── smoke_tools.py            # web/docgen 能力域离线冒烟(SSRF 面/构建器/路由回归)
│   ├── test_tools_flow.py        # web/docgen 能力域整栈验证(检索/生成下载/回归)
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

权重落 `data/tei_models/BAAI/bge-reranker-v2-m3/`(已 gitignore, 单文件 fp32 \~2.2GB, 勿用
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
# 文档知识图谱边的存量迁移(升级后必跑一次): 查询出口现在按 KG_REL.docs 做边级 ACL,
# 没溯源的历史边会隐身, 所以顺序必须是 迁移 → 重抽 → 再看图。
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.migrate_kg_edges --dry-run
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.migrate_kg_edges --apply
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.build_doc_kg
# 重刷业务数据(可选)
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.seed_business_data --force
# Web 聊天: http://localhost:18000   文档管理: http://localhost:18000/upload
#   (另有 我的记忆 /memory 与 知识图谱 /graph; 原 /docgen 创作工坊页已随网页链路下线)
# Neo4j Browser: http://localhost:17474   (bolt: localhost:17687; 原 7687 已落进本机 winnat 排除段)
```

> **宿主发布端口对照(当前实测)**:
>
> | 服务                                                                                                       | 宿主端口                                                                        | 容器内监听      | 说明                                          |
> | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | --------------- | --------------------------------------------- |
> | assistant                                                                                                  | `18000`                                                                         | `8000`          | Web 聊天 / API / 文档管理                     |
> | hr-mcp / finance-mcp                                                                                       | `18001` / `18002`                                                               | `8001` / `8002` | MCP 服务                                      |
> | analytics-mcp / procurement-mcp                                                                            | `18005` / `18006`                                                               | `8005` / `8006` | 数据洞察 / 采购合同 MCP                       |
> | askdata-mcp                                                                                                | `18007`                                                                         | `8007`          | 智能问数 MCP(语义层/编译/取数)                |
> | neo4j HTTP                                                                                                 | `17474`                                                                         | `7474`          | Neo4j Browser 网页控制台(人工看图谱时才需要)  |
> | neo4j Bolt                                                                                                 | `17687`                                                                         | `7687`          | 驱动 RPC, 图记忆/文档知识图谱走这个(功能必需) |
> | tei-rerank                                                                                                 | `8080`                                                                          | `8080`          | 重排服务 `/rerank`与`/health`                 |
> | redis                                                                                                      | `16379`                                                                         | `6379`          | redis-stack-server, 记忆层/缓存层共用         |
> | postgres / ES / Mongo / mineru / hr-agent / finance-agent / analyst-agent / contract-agent / askdata-agent | `5432` / `9200` / `27017` / `8888` / `9001` / `9002` / `9005` / `9006` / `9007` | 同左            | 未抬, 保持原值                                |
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
> 2026-10 实测又新增 `6234-6333 / 6334-6433 / 6596-6695 / 6748-6847`, 把 Redis 的 `6379`
> 括了进去(表现为 redis `Exited(255)` 无应用日志, 记忆层/缓存层静默退回内存), 已按同一
> 约定抬到 `REDIS_HOST_PORT=16379`。
> 排查命令: `netsh int ipv4 show excludedportrange protocol=tcp`。若某个服务因端口起不来,
> 其他容器里会报 `Failed to DNS resolve address <svc>:<port> ([Errno -2])` —— Docker 内置 DNS
> 不解析已退出容器的服务名, 根因不在报错那一层。
> 换机后若 `18xxx`/`17xxx` 也被排除, 改 `docker/.env` 里对应的 `*_HOST_PORT`/`TEI_PORT` 再抬一段
> 即可, 无需改 compose 默认值(见 `docker/docker-compose.yml` 顶部约定)。

### 本地开发

开发/验证期的拓扑约定(2026-09 起, 见 `CONFIG_RULES.md` 第 9 条与
`.qoder/rules/container-first-verification.md`): **容器是唯一的验证环境** ——
assistant 网关与全部依赖都在 compose 里跑, **宿主机禁止直跑网关做代码验证**;
宿主只跑 vite dev(前端页面) 与 Ollama(唯一非 docker 依赖)。改过 `app/` 任何后端
代码都要 `-Build` 重建镜像才生效(镜像层缓存了依赖, 重建通常只重 COPY)。

```bash
# 0. 依赖与环境(pip 仅用于引导 uv; 宿主侧仅装管理脚本/评测脚本所需的 dev 依赖)
pip install uv
uv sync

# 1. 首次开发: 从模板建两份配置 + 密钥目录(模板入库, 真实值不入库)
cp .env.example .env                      # 宿主轨: 供宿主侧脚本(dev_services check/init_db/评测)连 docker 已发布端口
cp docker/.env.example docker/.env        # 容器轨: 服务名 + /data 卷路径 + compose 插值
# 密钥只写 docker/secrets/<name>.txt(见该目录 README.md), 不写进任何 .env

# 2. 建表自检(含 CREATE EXTENSION vector); Mongo 集合索引由网关启动幂等创建
uv run python -m scripts.init_db

# 3. 一键起: docker 全栈(含 assistant 容器网关) + 对接自检 + 前端(:5173)
./scripts/dev.ps1
./scripts/dev.ps1 -Build                          # 改过 app/ 后端代码后的标准动作
uv run python -m scripts.dev_services check --gateway   # 只看对接结果(含探容器网关, 幂可随时跑)

# 4. 页面入口(开发期一律走 vite dev, 不依赖 web/dist)
#   聊天 http://localhost:5173   文档上传 http://localhost:5173/upload
#   记忆 http://localhost:5173/memory   知识图谱 http://localhost:5173/graph
#   API 文档 http://localhost:18000/docs   健康检查 http://localhost:18000/api/health

./scripts/dev.ps1 -Stop                            # 结束 vite 并停掉 docker 全栈(含网关容器)
uv run python -m scripts.dev_services down         # 只停 docker 服务(只停不删卷)
```

`dev.ps1` 等价的手工三步(想单独控制某一层时用):

```bash
docker compose -f docker/docker-compose.yml up -d --build \
  assistant ingest-worker postgres elasticsearch redis neo4j mongo tei-rerank mineru hr-mcp finance-mcp analytics-mcp procurement-mcp askdata-mcp hr-agent finance-agent analyst-agent contract-agent askdata-agent
uv run python -m scripts.dev_services check --gateway    # 对接结果 + 容器网关健康
cd web-ui && pnpm install && pnpm dev                    # 前端 dev, /api 代理到 assistant 容器发布端口(18000)
docker logs -f assistant                                 # 网关日志(不再落宿主文件)
docker logs -f ingest-worker                             # 入库 worker 日志(阶段推进/回收/GC)
```

要点:

- **网关只在容器里**: `dev_services up` 默认含 `assistant`; 宿主直跑 `uvicorn app.main:app`
  做验证属违规(宿主轨与容器轨配置视角不同, 宿主结论对容器部署不成立)。
- **配置分两轨**: `app/config.py` 只读宿主侧 `.env`/`.env.local`(仅供宿主脚本连 docker
  发布端口); 容器侧靠 compose 的 `env_file: [.env]`(= `docker/.env`) + `environment:` 注入,
  且红线键已**字面量锁死**, 改 docker/.env 地址不再生效(防覆盖, 见 `CONFIG_RULES.md` 第 10 条;
  专项自检 `uv run python -m scripts.dev_services env-check`)。两侧地址不同是**故意的**,
  任何一侧混入另一侧的地址都会让对应层静默降级(第 5 条)。
- **降级是静默的**: Redis/Neo4j/TEI/Mongo/ES 连不上时功能照跑, 只是退回内存态或 RRF 融合序。
  所以改了配置先跑 `dev_services check`, 别看页面表现猜。
- **改过任何后端代码要重建镜像**: 容器里跑的是镜像快照(含 assistant 自身),
  用 `./scripts/dev.ps1 -Build`; 没有热重载兼容层, "改了没反应"先查是否忘了 -Build。
- **SSE 在 dev 模式可用**: vite 代理是 http-proxy 非缓冲透传, `POST /api/chat/stream` 与断点续传
  都正常:`MXI_BASE=http://127.0.0.1:5173 uv run python scripts/test_sse_resume.py`。
- **前端 pnpm v11 只认 `web-ui/pnpm-workspace.yaml`**: v11 起 `package.json` 的 `pnpm` 字段不再被
  读取(命中会打 `[WARN] The "pnpm" field in package.json is no longer read`), `.npmrc` 也只留
  认证/registry —— 所以构建脚本白名单写在 `pnpm-workspace.yaml` 的 `allowBuilds`(v10 的
  `onlyBuiltDependencies` 已被它合并取代), esbuild 必须列在里面, 否则 Vite 二进制没装配好。
  `shamefullyHoist`/`strictPeerDependencies` 同样只能写在 yaml 里; 旧的 `.npmrc` 写法(以及历史上
  的 `shamefully-hoist=true`)在 v11 下**静默失效**, 现存 `node_modules` 就是默认隔离布局 ——
  真打开会换掉依赖目录结构, 需要删 `node_modules` 重装, 属行为变更而非配置润色。
- **前端 pnpm 版本已钉死**(`web-ui/package.json` + `pnpm-workspace.yaml`): `packageManager:
"pnpm@11.9.0"` 管版本不符(`pmOnFail: download` → 自动改用声明版本, 也可临时 `--pm-on-fail=error/warn
/ignore` 覆盖), `engines.pnpm: ">=11.9.0"` 兜住旧版(pnpm 9/10 跳 `ERR_PNPM_UNSUPPORTED_ENGINE`
  硬失败, 不会静默装出另一种 node_modules 布局)。升级 pnpm 大版本时两个字段跟着改, 且得确认
  lockfile 版本与 `allowBuilds` 写法仍适用。

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

### Langfuse 自托管可观测(与 LangSmith 并存的独立开关)

第二条 trace 通道, 面向"想把调用链/成本/token 统计留在自己基础设施里"的场景。
与 LangSmith 的关键差别: Langfuse 跑在同一 compose 网络内(`profile=langfuse` 的
6 个容器), 对话数据不出本机, 因此**容器侧也可以开**; 而 LangSmith 传向外部云端,
容器侧恒锁 `false`。两者互不影响, 可只开一个、都开、或都关(默认全关)。

```powershell
# 1) 单独起 Langfuse 栈(默认不随 dev.ps1 起, 需显式带 profile)。
#    只点名 web+worker: 依赖的 4 个存储由 depends_on 自动带起。
#    切勿省略服务名跑裸 --profile langfuse up -d —— 那会把没挂 profiles: 的业务栈
#    (assistant/postgres/各 agent...) 一起拉起, 不是"单独启动 Langfuse"。
docker compose -f docker/docker-compose.yml --profile langfuse up -d langfuse-web langfuse-worker
# 2) 浏览器开 Web UI http://localhost:18100 -> 注册 -> 建 org/project -> 取 pk-lf-/sk-lf-
#    写入 docker/secrets/langfuse_api_key.txt(第一行 sk, 第二行 pk)
# 3) docker/.env 置 LANGFUSE_ENABLED=true 并重建 assistant 镜像
./scripts/dev.ps1 -Build
```

secret 文件是在容器创建时才解析挂载点, 如果文件是后来才补的, 记得
`docker compose -f docker/docker-compose.yml up -d --force-recreate assistant`
(否则 `/run/secrets/langfuse_api_key` 是个空目录, 密钥读不到; 日志会给出明确告警)。

要点:

- 接入点是 `app/tracing.py::langfuse_callback()`, 在图顶层 `ainvoke` 的 config 上挂
  LangChain `CallbackHandler`; 回调沿 LangGraph 传播到所有子 run(意图识别/RAG/ReAct
  工具/MCP/A2A), 一轮对话归并为**一条 trace**。
- trace id 由 `audit.jsonl` 的 `trace_id` 经 `Langfuse.create_trace_id(seed=...)` 确定性
  派生, 且原始 `trace_id` 也落进 trace metadata(`mxi_trace_id`): 从审计日志可直接跳到
  Langfuse 对应 trace, 反之亦然。
- 一条命令验证整条链路(先按上面三步启用):

  ```powershell
  docker compose -f docker/docker-compose.yml run --rm --no-deps assistant `
      python scripts/smoke_langfuse.py
  ```

  未启用时会打印 FAIL + 具体原因(开关 / 密钥 / 地址), 不抛栈。

- 读回接口注意: Langfuse v4 默认 `events_only` 写入模式, v3 时代的 `/api/public/traces`
  列表/详情接口已不可用(实测 404); 程序化读回走 `observations` 接口(按 `trace_id`
  取全链路节点, `session_id`/`user_id`/`environment` 就在上面)或直接看 UI。
  镜像默认 `langfuse/langfuse:4`, 想回到 v3 体验只改 `LANGFUSE_IMAGE`/`LANGFUSE_WORKER_IMAGE`。
- 降级口径与全仓一致: 未启用/未配密钥/没装 langfuse 包 -> `langfuse_callback()` 返回
  空 dict, 图调用 config 里 `**` 展开即无操作, 不影响业务链路; 上报失败只告警。
- Langfuse 栈的存储(PG/ClickHouse/redis/MinIO)与业务的 postgres/redis 完全隔离,
  删栈不伤业务数据; 详见 `docker/docker-compose.yml` 内注释。

### 环节级评测(一条 trace 里定位「哪个环节掉链子」)

只有一条 trace 时, 质量问题混在一起看不清: 是意图路由错了, 还是压根没检索到,
还是检索到了没引用上。`app/tracing.py::stage_eval()` 因此给四个核心环节各建一个
`eval.<环节>` span, 把**规则能确定性判定**的信号按 [EVAL_METRICS.md](EVAL_METRICS.md)
的指标编号上报成 score:

| 环节 | score name | 类型 | 读法 |
| --- | --- | --- | --- |
| `classify_intent` | `RT-03.intent_layer` | CATEGORICAL | 取值分布 = 四层漏斗各层命中占比 |
| | `RT-04.intent_confidence` | NUMERIC | 分布漂移 -> 重标定 0.62/0.05 阈值 |
| | `RT-02.intent_target` | CATEGORICAL | 域判定结果 |
| `kb_retrieve` | `RET-DEG-01.rerank_degraded` | BOOLEAN | 均值 = 精排降级率(降级时阈值不生效) |
| | `RET-CACHE-01.cache_hit` | BOOLEAN | 均值 = 检索缓存命中率 |
| | `RET-ACL-01.acl_dropped_docs` | NUMERIC | 检索出口仍剔除的越权/未就绪文档数 |
| | `RET-RERANK-07.top_score` | NUMERIC | 保留块最高分, 阈值 0.4 重标定的依据 |
| | `RET-RERANK-04.empty_after_threshold` | BOOLEAN | 均值 = 被阈值裁空的轮次占比(直接导向拒答) |
| `kb_generate` | `GEN-REFUSE-01.refused` | BOOLEAN | 拒答/正常生成两个出口都打, 分母才完整 |
| | `GEN-TOK-01.context_chars` | NUMERIC | 上下文规模(无 tokenizer, 字符作代理口径) |
| | `GEN-CITE-01.citations_in_answer` | NUMERIC | 答案里 `[资料N]` 标注条数(覆盖率分子) |
| `agent_delegate` | `RT-14.delegate_status` | CATEGORICAL | ok/denied —— denied 时 answer 也非空, 不能靠它判成败 |
| | `PERF-02.a2a_latency_ms` | NUMERIC | A2A 往返耗时(含远端 ReAct) |

- **为什么不直接用 `score_current_span()`**: 实测在 LangGraph 节点函数体内 OTel
  current span 无效(`CallbackHandler` 在 `on_chain_start` 里 attach 的 context 不跨
  Pregel 的执行边界进节点), 该方法会**静默 no-op** 而调用方看不出失败。所以环节
  span 用 `trace_context` 显式绑回同一条 trace(seed 与 `langfuse_callback` 一致)。
- **口径边界**: 意图准确率(RT-01)、忠实度(GEN-FAITH-01)这类需要标注集或 judge
  的指标**不在这条通道**, 仍走离线评测; 在线只报规则能判定的量, 以及重标定时赖以
  判断分布的观测值。不要拿 `GEN-CITE-01.citations_in_answer` 当引用正确率用。
- **验证落点**: 实跑一轮后 `default.scores` 表可直接查(每个环节一个独立
  `observation_id`):

  ```powershell
  docker exec docker-langfuse-clickhouse-1 clickhouse-client --query `
    "SELECT name, value, observation_id FROM default.scores ORDER BY timestamp DESC LIMIT 15"
  ```

  `scores` 的读回 REST 接口在 v4 `events_only` 模式下同样 404, 只能走 ClickHouse 或 UI。

## 端到端链路("我要报销")

1. `POST /api/chat` → Assistant 载入会话记忆(短期窗口 + 长期摘要)与个人级记忆各桶
   (开关开启时);问题含相对时间时, 先在进程内取平台当前时间并注入后续 Prompt
2. `rewrite_query` 消解指代后走意图三层漏斗 —— "我要报销"这类高频固定指令在第一层
   (规则)就短路命中 → `agent_delegate / finance`, 零网络调用
3. 权限校验(角色白名单)→ A2A Client 拉取 Finance_Agent 的 Agent Card(卡片**通告地址不作
   路由依据**, 统一按配置端点覆盖)并 `message/send`
4. Finance_Agent(LangGraph ReAct + deepseek-flash)追问/补齐要素后,经 MCP 调用
   `create_reimbursement` 创建报销单
5. 单号/审批节点沿 A2A 返回 → Assistant 回复用户;全程写 `logs/audit.jsonl`
   (同一 trace_id),敏感字段(金额/证件号/手机号)脱敏;下载链接/URL 段原样保留
   (否则脱敏会把链接撕成坏链)。

K8s 部署:把 `docker/docker-compose.yml` 里的各服务(网关/四个 MCP/四个 Agent 与
postgres/ES/Redis/Mongo/Neo4j/TEI/MinerU 依赖)逐个映射为 Deployment+Service
(应用类服务可用 `kompose convert` 直接转换后人工校对),有状态组件建议换集群托管存储;
Ollama 建议独立部署为推理服务,
`OLLAMA_BASE_URL` 指向其集群内 Service 地址即可,应用代码无需改动。

## 并发容量(多少人共用一个网关要看哪些数)

网关是单进程异步服务: 代码里任何一处"每请求新建客户端/每请求编译一张图/在事件循环里做
同步 IO"都会被人数乘出来。下面这些键不是功能开关, 而是容量旋钮 —— 调小的现象不是报错,
而是静默排队到超时(所以出了问题先看日志里的降级告警, 而不是等 500)。

| 键                                                          | 默认         | 卡住的是什么                                                                  |
| ----------------------------------------------------------- | ------------ | ----------------------------------------------------------------------------- |
| `PG_POOL_SIZE` / `PG_MAX_OVERFLOW` / `PG_POOL_TIMEOUT`      | 30 / 20 / 15 | 异步引擎连接池(SQLAlchemy 默认只有 5+10)                                      |
| `PG_SYNC_POOL_SIZE` / `PG_SYNC_MAX_OVERFLOW`                | 5 / 5        | 同步引擎(psycopg3); 网关 + 每个 mcp/agent 进程各建一份                        |
| postgres `max_connections`                                  | 200          | 上面两项的总和必须留得下, 对账口径见 `app/config.py::pg_pool_size` 注释       |
| `REDIS_MAX_CONNECTIONS`                                     | 100          | 会话记忆 + 三类 Cache + Checkpointer 共用一个客户端(默认 50)                  |
| `EMBEDDING_QUERY_CONCURRENCY` / `EMBEDDING_MAX_CONNECTIONS` | 8 / 32       | Ollama 推理并发: 超过它在内部 tokenize 阶段返 400, 语义层与稠密通道会集体降级 |
| `ES_SEARCH_TIMEOUT` / `ES_SEARCH_CONCURRENCY`               | 3s / 32      | 稀疏通道: 原来是 30s 挂钟, ES 半死时把连接和内存拖爆                          |
| `RERANK_TIMEOUT` + TEI 连接池                               | 3s / 16      | 重排在热路径上, 超即降级 RRF 融合序                                           |
| `A2A_TIMEOUT` / `A2A_MAX_CONNECTIONS`                       | 120 / 64     | 委派是多步办理, 没有墙钟就是一条永不返回的请求                                |
| `MCP_TOOLS_TTL`                                             | 300s         | 工具清单缓存: 否则每次 tool_call 都重开一条 MCP 会话去 discover               |
| `LLM_REQUEST_TIMEOUT` / `LLM_MAX_RETRIES`                   | 120 / 2      | 在线供应商挂起时不能长期占住一个并发额度; 重试放大会把 429 放大成雪崩         |
| `STREAM_MAX_CONCURRENT_RUNS`                                | 200          | 背压闸门: 超上限直接 503(+`Retry-After`), 保护已经在流的人                    |
| `STREAM_MAX_EVENTS` / `STREAM_MAX_BUFFERS`                  | 4000 / 2000  | 单个 run 的事件数与缓冲区总数的内存硬顶                                       |
| `THREAD_POOL_TOKENS`                                        | 64           | `asyncio.to_thread`(默认 min(32,cpu+4))与 anyio(默认 40)两个线程池            |
| `SESSION_MEMORY_LOCAL_MAX`                                  | 5000         | Redis 不可用时进程内会话记忆的 LRU 上限(防无界涨内存)                         |

单 worker 部署下真正的天花板依次是: 在线 LLM 的并发配额 → 一份 Ollama/TEI 的推理吞吐 →
PG 连接总量。闸门取 200 的含义是"到此为止接新流量", 不是"能同时服务 200 人"。

验证手段(不起宿主网关, 压的是 **容器内** 网关, 见 `.qoder/rules/container-first-verification.md`):

```powershell
# 纯逻辑并发不变量(事件缓冲封顶与跳号提示、审计批量落盘、背压闸门、fetch 流式读取)
uv run python -m scripts.test_concurrency_offline
# 真打并发(默认 120 路流式对话; --clients N --kind chat|kb)
uv run python -m scripts.stress_concurrency --clients 200 --kind kb
# 就绪探针(过载时返回 503, 供负载器摘流量)
curl http://localhost:18000/api/health/ready
```
