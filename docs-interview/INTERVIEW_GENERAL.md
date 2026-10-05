# 面试题与逐字稿答案（通用工程 / 深挖题）

> 覆盖 Python 异步、数据库、配置与安全、前端联调，以及"最难的事/能不能上线"这类压力题。
> 每题都锚定到本项目的真实实现，避免答成通用八股。

---

## 一、Python 异步与 FastAPI

### Q1. 项目里全链路 async，踩过哪些异步相关的坑？

逐字稿：
四个印象最深的：

1. **asyncio 只弱引用 Task**。流式入口要把图执行丢到后台跑，如果我写 `asyncio.create_task(...)` 而不持强引用，任务可能被 GC 掉，表现是"偶尔这一轮静默没跑完"。我在 orchestrator 上挂了一个 `self._stream_tasks: set[asyncio.Task]` 存强引用，并靠 done 回调 discard。
2. **同步库必须卸载到线程池**。`ddgs` 是同步 HTTP 库，我在异步工具里用 `asyncio.to_thread` 包；SSRF 护栏里的 `socket.getaddrinfo` 也是同步 DNS，同样 `to_thread`，否则会卡事件循环——这类"一个同步调用拖死整轮并发"的问题在压测时才暴露，很难在开发机上看见。
3. **构造期不做 I/O**。所有 store/DAO（`ChunkStore`、`UserProfileStore`、`ChatStore`）都是懒拿 session factory，构造零 I/O；LangGraph 的图也不在 `__init__` 里编译，因为 checkpointer 需要 `await` 建索引而 `__init__` 是同步的，所以我用一个幂等的 `await setup()`，由 FastAPI lifespan 主动调一次，`handle()` 再兜底调一次（这样 LangGraph Studio 跳过 lifespan 也能拿到已编译的图）。
4. **超时要有分层预算**。httpx 的重排客户端是 `timeout=3.0, connect=0.5`——只设总超时不够，服务半死时建连阶段就能吃掉全部预算。子任务并发用 `asyncio.gather` + 每个子任务单独超时，超时只让那一节降级成"未完成"，不整轮报错。

### Q2. 为什么高并发 HTTP 调用要复用 AsyncClient？你在哪里踩过？

逐字稿：
`httpx.AsyncClient` 内部是连接池；每次请求都新建客户端等于每次重新 TCP 握手 + TLS 协商，短连接会堆 TIME*WAIT，在高 QPS 下先把本地端口耗尽再把延迟抬起来。我把重排、联网检索/抓取都做成了**进程级单例客户端**（重排的 `_get_client()` 用 module-level 全局 + 幂等的 `close*\*`挂在 lifespan 关停；抓取侧共享池并显式`follow_redirects=False` 以便逐跳过 SSRF 护栏）。

具体踩在重排上：热路径每条查询要给十几个候选打分，如果 1+N 串行调用会把延迟放大一个数量级；所以除了共享池，我还把"所有候选一次批量 POST"作为硬约束，顺便对齐 TEI 的 `--max-client-batch-size` 做单批 32 的截断。

### Q3. 事件循环里跑数据库事务，事务边界怎么控？

逐字稿：
一个具体的：pgvector 检索前我要 `SET LOCAL hnsw.ef_search = ...`，`SET LOCAL` 只在当前事务里有效，而 SQLAlchemy 的 async session 会隐式开事务。如果我不收尾，这个 `ef_search` 会跟着连接回池、影响后续别的请求。所以我在查询结束后显式 `await session.rollback()` 来结束这个隐式事务。

但这里有个连带的坑，而且我踩过两次：**rollback 会 expire 所有 ORM 实体**，之后再去取实体属性会拿到过期对象甚至 `DetachedInstanceError`。正确做法是在 session 关闭前把结果转成 DTO——我的 `search()` 就是在循环里立刻用 `_chunks_from_narrow(row, score)` 构造 `KnowledgeChunk`，出函数只带 DTO。历史上"个人记忆召回静默变空"这个 bug 的根因就是 rollback 之后才取 ORM 属性。

写入侧我一律用 `async with session.begin()` 显式包事务，批量 upsert 按 500 行分批（`ON CONFLICT DO UPDATE`），避免一条语句参数过多；父子块发布必须是**一个事务**（先 upsert 再剪陈旧，先子后父），否则就出现检索空窗。

---

## 二、数据库与存储

### Q4. Pydantic / pydantic-settings 在你项目里承担了什么？配置优先级怎么定？

逐字稿：
`Settings(BaseSettings)` 是全站配置的唯一入口，`@lru_cache` 单例。优先级是**真实环境变量 > `.env.local` > `.env` > 字段默认值**。

这里最重要的一条经验是：**pydantic-settings 的 `env_file` 元组是后者覆盖前者**。我早期写的是 `(".env", "docker/.env")`，意图是"读两份"，实际效果是 docker 那份（全是容器服务名 `tei-rerank`/`elasticsearch`/`mongo`）把宿主端口盖掉，宿主进程解析不到这些名字，然后**静默降级**——页面能聊，但重排/ES/Mongo 全没接上。现在只读宿主轨 `.env`/`.env.local`，容器侧完全交给 compose 的 `env_file` + `environment` 注入，并且把"不许把 `docker/.env` 加回 env_file"写成了配置红线。

另外几个细节：

- 密钥字段 `Field(default="", repr=False)`，防止 `print(settings)`、异常栈、trace 把密钥带进日志。
- `validate_default=True`：这样即使环境变量缺失、字段取默认值，也会跑我的 `field_validator` 去回退读 `/run/secrets/<name>`，再退 `docker/secrets/<name>.txt`；文件里是 `REPLACE_ME` 占位值时视同未配置，让调用侧报"缺密钥"而不是带着占位值去请求收 401。
- 多密钥单文件（Langfuse 的 sk/pk 同一个文件两行）不能在 `field_validator` 里拆——另一个 after 校验器会把整份内容盖回来，我放在 `model_post_init` 里拆一次。

### Q5. asyncpg 连不上 / 意外走 TLS 是怎么排查的？

逐字稿：
这是"默认行为反过来咬你"的典型案例。asyncpg 在不显式传 `ssl` 时可能自己做 TLS 协商，遇到自签证书的内网 PG 实例就会连不上，而且报错信息不指向 SSL。修复是**显式传** `ssl=settings.pg_sslmode`（`disable`/`require`），并把 `timeout`、`server_settings={"application_name": ...}` 一起走 `connect_args`——`application_name` 很实用，PG 侧 `pg_stat_activity` 里能一眼区分是网关、迁移脚本还是评测进程。

顺带一条通用教训：历史注释里写过"MySQL/Milvus"、写过"knowledge_chunks 表"，代码跑得好好的但注释是错的——**数据库版本/方言这类声明不可信，必须运行时验证**（`SELECT version()`、`extversion`）。我在 `init_db` 和启动日志里都打印实际版本与对接地址快照，就是为了让"注释说什么"不重要。

### Q6. upsert / 批量写有什么方言坑？

逐字稿：
一个具体的：SQLAlchemy 里用 PG 的 `ON CONFLICT` 必须走 `sqlalchemy.dialects.postgresql.insert`（我在代码里叫 `pg_insert`），用通用 `insert()` 拼出来的语句不带 `ON CONFLICT`，或者 `on_conflict_do_update` 根本不可用。我把它封成 `_upsert_batch(session, dialect_insert, rows, pk_col, upsert_cols)`，父子两张表共用，`set_` 用 `stmt.excluded[col]` 整体覆盖。

另一个坑是**覆盖列的选择要精确**：子块的 upsert 列里必须包含 `chunk_text` 和 `embedding`（重入库要刷新），但允许 `vectors` 传 `None`——增量入库时未变化的块不重 embed，写 `None` 让原向量保留。这类"哪些列该覆盖、哪些不该动"的取舍一旦写错，表现是向量被清空或权限被回退。

---

## 三、前端联调与接口设计

### Q7. 前后端怎么协作的？开发态和生产态的差别你怎么处理？

逐字稿：
前端是 Vue3 + Vite + Element Plus，源码在 `web-ui/`，构建输出 `../web/dist`。网关的托管逻辑是**条件式**的：`web/dist/index.html` 存在才挂 StaticFiles 并做 SPA 回退；不存在时不再无条件回退 `index.html`（那样会让 `GET /` 直接 500，把"其实该起 vite dev"这个真实原因掩盖掉），而是返回一个 JSON 明确提示：dev server 地址、怎么起、API 文档和健康检查路径。

开发期一律走 vite dev：代理目标读宿主 `.env` 的 `ASSISTANT_PORT`（默认 18000），构建时若 `.env` 缺失（比如容器内构建）就退回默认端口而不是阻断前端启动。SSE 在 dev 下也能测，因为 vite 的 http-proxy 是透传不缓冲。

接口设计上我坚持三件事：

- 契约走 Pydantic schema（`ChatRequest`/`ChatResponse`），`route` 是 Literal 枚举，前端按 `route` 决定展示形态（知识库带引用来源、多智能体并发带 `metadata.agents` 逐个成败）；能选哪些智能体也不硬编在前端，`GET /api/agents` 按角色白名单给菜单。
- 会话历史要能完整回填：`/api/sessions` 列会话、`/api/sessions/{id}/messages` 给全部消息（含思考过程、路由、参考来源），这样断点续流的缓冲过期后前端有可降级路径。
- 交付物一律回**可点的链接**而不是裸路径：网关不把 `/api/...` 当静态路径暴露（否则用户上传的知识库原件会被连带暴露、还绕过权限），下载路由只作用域 `gen/<token>/` 子树并叠四层防护；prompt 里也明确告诉模型"必须真实调用 generate\_\* 拿到 download_url 再输出成 Markdown 链接，不要改写或截断地址"，因为前端不会把裸 `/api/` 文本渲染成可点链接。

---

## 四、压力题 / 深挖题

### Q8. 这个项目最难的地方是什么？（一定要给出"难在哪"而不是"做了什么"）

逐字稿：
最难的**不是任何单点技术，而是"降级正确性"这一整类问题**。

我的整套系统里有七个可失败的外部依赖：Redis、Elasticsearch、MongoDB、Neo4j、TEI、Ollama、在线 LLM。我选的策略是"能降级就降级、绝不阻断对话"——这在可用性上是对的，但它制造了一个更难的问题：**故障表现为"功能还在"，不表现为报错**。

举三个真实发生过的：

- 重排通道每条查询都在超时降级成 RRF 序，对话照常，只有 `score_mode` 这个审计字段能看出来；我从页面表现上看了很久都以为"重排效果一般"。
- 配置里混进了另一轨的地址（容器服务名进宿主进程），解析失败后那层静默退回内存态，看起来是"缓存没生效"，实际上是根本没连上。
- rollback 之后取 ORM 属性导致个人记忆召回为空，接口返回 200、字段是空列表，没有任何异常。

所以我为这类问题建了四件套，我觉得这是这个项目里最值得讲的部分：

1. **降级事实本身入审计**：`context_degraded`、`score_mode`、`cache_hit`、`acl_blocked` 这些字段专门用来区分"没生效"和"生效但没结果"。
2. **启动时打印依赖对接地址快照**，并做**语义探针**——重排不是查 `/health`，而是真打一次 `/rerank`，拿一条明显相关和一条明显无关的文本校验排序对不对，这能覆盖"服务活着但模型不对/分数全 0"。
3. **专用自检脚本**（`dev_services check` / `env-check`）而不是靠看日志。
4. **红线锁死**：把最容易被覆盖出问题的配置项写成字面量，并让自检去扫配置源本身（含 `${` 就 FAIL）。

难的地方在于，这些都不是"加个 try/except"能解决的：它要求你对每个外部依赖都想清楚三件事——**它挂了系统该怎么表现、这个表现怎么被观测到、怎么证明现在的"正常"不是"降级"**。

### Q9. 你怎么验证一次改动没把链路改坏？（考察测试与验证习惯）

逐字稿：
我分层验证，按成本从低到高：

1. **离线单测/纯函数测**：点选清洗与分节合并（`normalize_agent_targets`/`_merge_agent_answers`）、改写清洗（`_clean_rewrite`）、RRF 融合、SQL 护栏、spec 解析这些纯逻辑都能离线跑；有一支脚本专门在**不启服务**的情况下验 SSRF 面、构建器和路由回归（`smoke_tools`）。
2. **整栈冒烟**：`test_tools_flow` 走真实链路验检索与生成下载；`demo_reimburse` 跑"我要报销"的端到端委派；`test_multi_agent` 验多智能体并发委派（含用审计区间重叠证明真并发）；`test_sse_resume` 验断点续流的重放语义。
3. **检索质量回归**：MS MARCO 评测 + 切块 A/B，独立评测库/索引/Mongo 库，指标口径固定（评排序时阈值置 0），改召回逻辑前后能对数字。
4. **对接自检**：任何配置改动后先 `check`，确认七个依赖都在，而不是打开页面猜。

有两条纪律是踩过代价才立的：**改过 `app/` 必须重建镜像**再看结果（容器跑的是镜像快照，没有热重载）；**验证只在容器里做**，宿主跑通的结论对容器部署不成立。这两条听起来像流程洁癖，但实际上我至少三次因为"忘了 -Build"或"在宿主验的"得出过完全错误的结论。

### Q10. 如果明天要真的上生产，你还差什么？

逐字稿：
按优先级我会补五件事，其中前两件是"不上生产也必须先有"的：

1. **身份与鉴权**：现在 `ChatRequest` 直接带 `user_id/role/department`，内部逻辑完全信任它。生产必须换成统一身份系统签发凭证（JWT/OIDC）+ 服务端解析角色，否则任何人伪造一个 role 就能越过白名单。同时 `CORSMiddleware` 现在是 `allow_origins=["*"]`，生产要收紧到实际来源。
2. **数据库权限边界**：Text2SQL 目前靠应用侧黑名单 + 表白名单 + `statement_timeout`，生产必须加"只读角色 + 脱敏视图"，把能力关在数据库权限层而不是代码层。
3. **水平扩展**：SSE 事件缓冲和 Session Memory 的降级路径都是进程内的，多副本需要把缓冲外置（Redis Stream）或 sticky session；单 worker 是当前明确记录的约束。
4. **观测闭环**：审计是 JSONL 文件（注释里写了生产应扇出到 SIEM），要接集中式采集；Langfuse 自建栈的存储跟业务隔离这点已经做了，但告警和 SLO 还没有。
5. **评测与数据治理**：真实问答集 + LLM judge + 引用正确率人工抽检；以及文档保留期、删除合规（向量删除、Mongo 残留回收、令牌过期）。

顺便说一个我**故意没做**的东西：架构图里的 Project Memory（项目级共享记忆）我没实现。因为它涉及跨用户的记忆可见性边界，权限模型跟个人六桶完全不同，不能顺手复用同一张表，我宁可显式标注"未实现"，也不要在权限语义上留一个含糊的桶。

### Q11. 快速答题（面试官连珠炮时用）

| 问题                             | 一句话答案                                                                                             |
| -------------------------------- | ------------------------------------------------------------------------------------------------------ |
| 稠密通道用的什么距离？           | cosine 距离（`<=>`）升序，HNSW `vector_cosine_ops`，`m=16/ef_construction=64`                          |
| embedding 维度？                 | 1024（bge-m3），改模型必须同步 `EMBEDDING_DIM` 并全量重建                                              |
| 为什么 RRF 的 k=60？             | 论文经验值，作用是压平头部排名差距；只吃排名不吃分数，无需调权重                                       |
| rerank 阈值多少？                | 0.4，全链路唯一阈值，只作用于 rerank 后的 0~1 相关性                                                   |
| 检索候选与最终条数？             | 每通道 top_k=8，融合后 rerank 取 top_n=4                                                               |
| 意图语义层参数？                 | Top-3 相似度均值，命中阈值 0.62，margin 0.05                                                           |
| 多智能体并发参数？               | 可点选上限 3、并发 2、逐位超时 150s（内层 a2a 120s）、单节正文截断 4000 字                    |
| 会话记忆窗口？                   | 滚动 10 轮，超 20 条触发 LLM 摘要折叠，Redis TTL 1 小时                                                |
| 缓存 TTL？                       | Prompt/Retrieval 300s，Tool 30s（web 域 300s）                                                         |
| 情节召回窗口？                   | 近 30 天，超 90 天排序降权，蒸馏门槛 3 条新增情节                                                      |
| 切块参数？                       | 父块=章节，超 1200 字滑窗 512/overlap 64 切子块，单块上限 20000 字                                     |
| SSE 缓冲保留？                   | run 结束后 600s，过期前端降级拉历史                                                                    |
| 服务数量？                       | compose 16 个（网关 + 4 MCP + 4 Agent + 6 存储/推理），另有 profile=langfuse 的 6 个可选容器           |
| 宿主端口为什么抬到 18xxx/17xxx？ | Windows `winnat` 每次开机把整段端口写进 TCP 排除范围，段内无法 bind                                    |
| 密钥放哪里？                     | 只放 `docker/secrets/*.txt`，经 compose secrets 挂到 `/run/secrets/*`，dotenv 留空，打包有两道密钥闸门 |
| 唯一非 docker 依赖？             | Ollama（只做 embedding）；另一个宿主例外是前端 vite dev                                                |
