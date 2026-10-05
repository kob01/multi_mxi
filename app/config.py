"""Application settings loaded from environment / .env file."""

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 敏感项对应的 Docker secret 文件(BuildKit/compose secrets 挂载路径);
# 本地宿主机直跑时回退读取项目内 docker/secrets/<name>.txt (开发调试用)。
# 真实密钥只允许落在这两个位置之一, 禁止写进 .env / docker/.env。
_SECRET_FILES = {
    "pg_password": "/run/secrets/pg_password",
    "langsmith_api_key": "/run/secrets/langsmith_api_key",
    "langfuse_api_key": "/run/secrets/langfuse_api_key",
    "deepseek_api_key": "/run/secrets/deepseek_api_key",
    "zhipu_api_key": "/run/secrets/zhipu_api_key",
    "mongo_password": "/run/secrets/mongo_password",
    "tavily_api_key": "/run/secrets/tavily_api_key",
    "serper_api_key": "/run/secrets/serper_api_key",
}
_SECRET_HOST_FALLBACK = {
    "pg_password": "docker/secrets/pg_password.txt",
    "langsmith_api_key": "docker/secrets/langsmith_api_key.txt",
    "langfuse_api_key": "docker/secrets/langfuse_api_key.txt",
    "deepseek_api_key": "docker/secrets/deepseek_api_key.txt",
    "zhipu_api_key": "docker/secrets/zhipu_api_key.txt",
    "mongo_password": "docker/secrets/mongo_password.txt",
    "tavily_api_key": "docker/secrets/tavily_api_key.txt",
    "serper_api_key": "docker/secrets/serper_api_key.txt",
}

# LLM 供应商注册表默认值(单一事实源): dotenv 的 LLM_PROVIDERS_JSON 可整体覆盖,
# 解析失败时回退本表。字段含义见 Settings.llm_providers_json 注释。
_DEFAULT_LLM_PROVIDERS: dict = {
    # deepseek-flash / deepseek-reasoner: 官方集成 ChatDeepSeek(透出 reasoning_content);
    # 关闭思考时不传 reasoning_effort, 避免参数被服务端拒
    "deepseek": {
        "base_url_field": "deepseek_base_url",
        "api_key_field": "deepseek_api_key",
        "model_class": "langchain_deepseek:ChatDeepSeek",
        "thinking_template": (
            '{"enabled": {"thinking": {"type": "enabled"}, "reasoning_effort": "{effort}"}, '
            '"disabled": {"thinking": {"type": "disabled"}}}'
        ),
    },
    # glm-5.3-flash 等 GLM 系列: 智谱开放平台 OpenAI 兼容端点
    "glm": {
        "base_url_field": "zhipu_base_url",
        "api_key_field": "zhipu_api_key",
        "model_class": "langchain_openai:ChatOpenAI",
        "thinking_template": (
            '{"enabled": {"thinking": {"type": "enabled"}}, '
            '"disabled": {"thinking": {"type": "disabled"}}}'
        ),
    },
}


class Settings(BaseSettings):
    """Central configuration for the whole platform.

    取值优先级(高->低): 真实环境变量 -> .env.local -> .env -> 字段默认值。

    配置只有一条宿主轨: ``.env`` 是**宿主机视角**(全部指向 docker 已发布端口),
    ``.env.local`` 是同一视角的机器私有覆盖(均已 gitignore)。**不要**把
    ``docker/.env`` 加进 env_file: pydantic-settings 是后者覆盖前者, 那会把容器
    服务名(tei-rerank/elasticsearch/mongo/...)灌进宿主机进程(本轨现在只给
    dev_services/评测等脚本用, 网关已只跑在容器内), 解析失败后静默降级
    (见 CONFIG_RULES.md 第 5 条 → config-env-tracks §1)。
    容器侧一律靠 docker-compose 的 ``env_file: docker/.env`` + ``environment:``
    注入真实环境变量; 红线键在 compose 里已字面量锁死, 改 docker/.env 覆盖不动
    (见 config-env-tracks §7, 自检 ``scripts.dev_services env-check``)。
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        # 字段取默认值(环境变量缺失)时也运行校验器, 以便回退读取 /run/secrets/*
        validate_default=True,
    )

    # Ollama (仅 embedding 走本地 Ollama; rerank 已迁 TEI, 见下方 tei_rerank_url)
    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = "bge-m3"

    # DeepSeek 在线 API (OpenAI 兼容格式, 用于 LLM / 意图识别)
    deepseek_base_url: str = "https://api.deepseek.com"
    # 密钥优先取环境变量, 其次由校验器读取 /run/secrets/deepseek_api_key
    # repr=False: 防止 print(settings)/异常栈/LangSmith trace 把密钥带进日志
    deepseek_api_key: str = Field(default="", repr=False)
    # 智谱开放平台 (GLM 系列, OpenAI 兼容): 密钥只住 docker/secrets/zhipu_api_key.txt
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_api_key: str = Field(default="", repr=False)
    llm_model: str = "deepseek-flash"
    intent_model: str = "deepseek-flash"
    # 在线 LLM 请求上限: 一次对话轮要串/并发好几个 LLM 调用, 没有墙钟上限时一个挂住
    # 的服务端会长期占住一个并发闸门与一个 PG 会话(比一个坏答案贵得多)。这里不是
    # "让回答能等更久", 而是"卡死的调用必须死": 超后由现有降级路径接住。
    llm_request_timeout: float = 120.0
    # 重试次数取小: langchain 默认已会重试, 乘以人数就是配额翻倍; 供商 429/5xx 时
    # 重试风暴只会把故障放大, 宁可这轮降级也不能把下游再压一次。
    llm_max_retries: int = 2
    # 本地 Ollama 的同类墙钟上限: 回退分支也不能没有上限。本地模型冷加载/显存换入
    # 可以卡住分钟级, 没上限时一个挂住的请求会一直占住一个并发闸门, 直到把整个网关
    # 拖到对新请求排队。比在线值宽(本地推理本来就慢), 但不是"不设上限"。
    ollama_request_timeout: float = 300.0

    # ---------- LLM 供应商注册表 (模型前缀 -> 在线 API 路由) ----------
    # 切换在线大模型只需改 LLM_MODEL/INTENT_MODEL(双轨 .env), 不必动代码:
    # 命中某前缀 -> 走该供应商的 OpenAI 兼容端点; 全部未命中 -> 回退本地 Ollama。
    # 值里只允许 api_key="<settings 字段名>" 间接引用密钥, 禁止明文密钥进 dotenv。
    # 条目字段:
    #   base_url_field : 必填, 指向存 base_url 的 settings 字段名
    #   api_key_field  : OpenAI 兼容通道必填, 指向存密钥的 settings 字段名
    #   model_class    : "langchain 包:类名"; deepseek 必须用 ChatDeepSeek 才能透出
    #                    思考内容(langchain-openai v1 不提取 reasoning_content), 其他
    #                    OpenAI 兼容供应商用 ChatOpenAI 即可
    #   thinking_template: 思考开关 extra_body, 形如 {"enabled": {...}, "disabled": {...}}
    #                    的两个形态; 模板内 {effort} 替换为 llm_reasoning_effort。
    #                    不支持思考的供应商置 ""(不传 extra_body)
    llm_providers_json: str = json.dumps(_DEFAULT_LLM_PROVIDERS, ensure_ascii=False)

    # 意图识别三层漏斗: 规则快筛 -> bge-m3 语义分类 -> LLM 兜底。
    # 下面几项只作用于第二层(bge-m3 语义分类)的命中判定, 可按线上效果调。
    # 关闭即跳过第二层, 规则未命中直接下沉到 LLM。
    intent_embedding_enabled: bool = True
    # 每个意图取最相似的前 K 条种子, 用它们的相似度均值作为该意图得分。
    intent_semantic_top_k: int = 3
    # 最优意图得分需达到的最低相似度才视为命中(否则下沉 LLM)。
    intent_accept_threshold: float = 0.62
    # 最优意图需领先次优意图的最小差距, 防止边界样本在两类间摇摆。
    intent_margin: float = 0.05

    # ---------- 多智能体并发委派(用户显式点选) ----------
    # 复合问法自动拆分已下线(提示词口径难控), 改为调用方在 ChatRequest.agent_targets 里
    # 显式指定要并发委派的智能体域: 同一个问题下发给 N 个 A2A 专业智能体, 程序化拼成
    # "一节一个智能体"的 Markdown。哪些智能体参与由用户决定, 系统不做任何推断。
    # 总开关: 关闭即忽略 agent_targets, 回到"一句一个意图一条路由"。
    multi_agent_enabled: bool = True
    # 单次请求可点选的智能体上限, 超出部分不执行(正文末尾说明未处理项), 挡住
    # "一次选十个"把四个下游 ReAct 循环全打满。
    multi_agent_max_targets: int = 3
    # 并发上限: 一个委派是一整轮 ReAct(多轮 LLM + 多次工具调用), 比一次检索贵得多,
    # 默认只同时对两个智能体; 抬高前先确认各智能体进程与其 MCP 下游扛得住。
    multi_agent_parallelism: int = 2
    # 单智能体超时秒数: 超时只把那一节降级为"未完成", 不整轮报错(部分成功优于整轮失败)。
    # 取 150 > a2a_timeout(120): 让 send_guarded 那道内层超时先触发, 它回的降级文案比
    # 外层 TimeoutError 对用户更可读; 外层只做"内层也拦不住"兼顶。
    multi_agent_timeout: float = 150.0
    # 单个智能体答复进分节正文的截断长度: 并发 N 份长文本会把回答长度乘以 N,
    # 超上限部分截掉并给出提示(需要全文时改为只点选一个智能体)。
    multi_agent_answer_chars: int = 4000

    # 统一下沉到线程池的并发上限(同时受两个池约束, 见 app/main.py::_configure_thread_pools)。
    # 为何要显式抬: ``asyncio.to_thread`` 用事件循环默认线程池(上限
    # ``min(32, cpu+4)``), FastAPI 的同步接口/同步工具用 anyio 线程池(默认 40) ——
    # 文档生成/文档解析/PIL 图片归一化/同步 DB 工具全挤在这两个小池里, 不抬就是
    # 一人生成文档全厂排队。取 64: 与 PG 连接池/下游服务上限同量级, 再多只会是线程噪声。
    thread_pool_tokens: int = 64

    # RAG (向量块与业务元数据同库: PostgreSQL + pgvector; 现行为父子双表
    # doc_chunks(子块+向量) / doc_parents(父块结构, 正文在 Mongo), 旧单表 knowledge_chunks
    # 仅供迁移脚本读写, 见 scripts/migrate_doc_stores.py)
    knowledge_dir: str = "./data/knowledge"
    rag_top_k: int = 8
    rerank_top_n: int = 4
    # Rerank 走 TEI 容器的真 cross-encoder (/rerank), 不再借道 Ollama /api/embed。
    # 容器内必须用服务名 tei-rerank; 宿主轨默认 http://localhost:8080
    # (见 CONFIG_RULES.md 第 5 条 → config-env-tracks §2)。
    tei_rerank_url: str = "http://localhost:8080"
    # 总超时 3s / 建连 0.5s: TEI 半死不能拖垮整条对话链路(超时报错即降级 RRF)。
    rerank_timeout: float = 3.0
    rerank_connect_timeout: float = 0.5
    # 单条候选送打分前的字符截断, 控 batch token 规模(bge-reranker-v2-m3 上限 8k token)。
    rerank_max_chars: int = 1024
    # TEI 不可用/超时 -> 优雅降级为 RRF 融合序; 也可置 false 作为总开关跳过重排。
    rerank_enabled: bool = True
    # 全链路唯一的相关性阈值, 只作用于 rerank 阶段: rerank 分低于该值的块
    # 视为噪声剔除; 剔除后为空即"未检索到相关文档", 触发改写重检或拒答。
    # 检索通道(稠密向量 / ES BM25)与 RRF 融合均不设阈值, 只负责召回。
    # 分数标度: 真 rerank 输出 TEI sigmoid 后的 0~1 相关性(相关块常 >0.9, 噪声常 <0.05),
    # 与旧的 Ollama cosine 标度不同; 降级为 RRF 时本阈值不生效(见 HybridRetriever)。
    retrieval_score_threshold: float = 0.4
    # 无结果重检的最大次数 (0 = 关闭重检, 只检索一次)
    retrieval_max_retries: int = 1

    # Elasticsearch (BM25 稀疏检索通道; 中文经 jieba 预分词, 无需分词插件)
    # 容器内访问 compose 服务用 http://elasticsearch:9200 (见 docker-compose)。
    es_url: str = "http://localhost:9200"
    es_index: str = "kb_chunks"

    # ---------- 统一存储层: PostgreSQL (业务/文档元数据 + pgvector 向量检索) ----------
    # DATABASE_URL 优先(形如 postgresql+asyncpg://user:pw@host:5432/db); 为空时由
    # 下面的分散字段拼出 DSN (见 app/db/session.py / app/db/sync.py)。
    database_url: str = ""
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "mxi"
    pg_password: str = Field(default="", repr=False)
    pg_database: str = "mxi"
    # disable: 本机/compose 内网直连; require: 自签证书云实例(只加密不校验 CA)。
    pg_sslmode: str = "disable"
    pg_connect_timeout: int = 10
    # Text2SQL 语句级超时, 替代 MySQL 的 MAX_EXECUTION_TIME hint
    # (由 app/db/sync.py 在执行前 SET LOCAL statement_timeout 注入)。
    pg_statement_timeout_ms: int = 5000
    # ---------- 连接池容量(高并发唯一入口预算, 见下方容量对账注释) ----------
    # SQLAlchemy 默认 pool_size=5 / max_overflow=10 —— 那是"单进程个位数并发"的假设,
    # 百人共用一个网关进程时每个热路径(检索回表 / ACL 门禁 / 元数据 / 会话落库 /
    # 画像读写)都要抢这 15 条连接, 抢不到的排队 30s 后抛错, 表现为整站静默降级。
    # 预算必须和 PG 服务端的 max_connections 一起算(容器里 PG 默认 100):
    #   网关 async 池 (pool_size + max_overflow) + 网关 sync 池 + N 个 mcp/agent 进程
    #   各自的 sync 池 <= max_connections - 10(超级用户/巡检预留)。加一侧就要减另一侧。
    pg_pool_size: int = 30
    pg_max_overflow: int = 20
    # 等池超时秒数: 宁可等一小会儿也不要立刻抛, 但绝不能无限等(会把整条对话挂死)。
    pg_pool_timeout: int = 15
    # 同步引擎(psycopg3): 进程内工具(如 lookup_employee_by_name)在网关里也用它,
    # 容量要小 —— 它是"网关 sync + 各 mcp/agent 进程"这份预算里的乘数项。
    pg_sync_pool_size: int = 5
    pg_sync_max_overflow: int = 5
    # bge-m3 稠密向量维度; 换 embedding 模型必须同步改这里并全量重建向量表。
    embedding_dim: int = 1024
    # 单次 ON CONFLICT upsert 的行数 (避免单语句参数过多)。
    upsert_batch_size: int = 500

    # ---------- 正文外置存储: MongoDB ----------
    # 整篇 raw_text/normalized_text/structure 与父块全文存 Mongo;
    # PG 只存向量 + 窄标量 + 子块 chunk_text。热路径按 parent_id 精确取, 不做全文查询。
    # 关闭(MONGO_ENABLED=false)则父块上下文降级为子块文本(不阻断对话), 入库则直接报错。
    mongo_enabled: bool = True
    mongo_url: str = "mongodb://localhost:27017"
    mongo_database: str = "mxi"
    # 可选鉴权: 内网单节点默认无认证(对齐 ES/Neo4j); 云部署启用时走 URI 或这两项。
    mongo_user: str = ""
    mongo_password: str = Field(default="", repr=False)
    mongo_max_pool_size: int = 50
    mongo_server_selection_ms: int = 3000
    mongo_connect_timeout_ms: int = 5000
    # Mongo 单文档 16MB 硬上限: 整篇正文超过该阈值时溢出到 doc_body_parts 分片。
    mongo_body_max_bytes: int = 15_000_000
    # 批量读写分页大小(Mongo $in 分块 / bulk_write 批次 / chunk_text 主键批量 / 语料流式页)。
    mongo_batch_page_size: int = 500

    # ---------- 文本规范化与入库 ----------
    # offset 基准是 normalized_text; 归一化规则一改, 全库旧 offset 集体失效。
    # 任何修改 normalize_text() 的提交必须同步递增此值(见 CONFIG_RULES.md)。
    normalizer_version: str = "n1"
    # 增量入库: 块 content_hash 未变则不重新 embed(embedding 走 Ollama, 是最慢一环)。
    rag_incremental_ingest: bool = True

    # Document upload & metadata
    upload_dir: str = "./data/uploads"
    upload_max_mb: int = 50
    # 创作产物(图表 SVG/PNG / 周期报告 Markdown / 表格 CSV)的落盘目录; 容器侧指向持久卷
    # /data/reports (与 UPLOAD_DIR 同源, 否则写在容器工作目录重启即丢)。
    report_dir: str = "./data/reports"
    # MinerU OCR 服务 (图片/扫描件解析)。容器内由 compose 的 mineru 服务提供
    # (compose 注入 http://mineru:8888); 宿主轨默认指向已发布端口 8888。服务不可用时
    # 图片解析直接报 RuntimeError(上传接口转 502), 不静默丢内容(见 app/docs/parsers.py)。
    mineru_base_url: str = "http://localhost:8888"
    mineru_backend: str = "pipeline"
    mineru_timeout: int = 300
    # Parent-child chunking: a section block larger than this is window-split
    # into child chunks; smaller blocks stay a single child under the parent.
    parent_chunk_max: int = 1200

    # Assistant service
    assistant_host: str = "0.0.0.0"
    assistant_port: int = 8000
    memory_max_turns: int = 10
    memory_summary_threshold: int = 20
    # 深度思考全局默认 (deepseek-flash 默认开启思考; 单次请求可用 thinking 字段覆盖)。
    # 关闭后生成节点走一次性 ainvoke, 不产出 reasoning_content。
    llm_thinking_enabled: bool = True
    # DeepSeek 思考强度: high / max (flash 仅支持这两档, 无 low)。
    llm_reasoning_effort: str = "high"
    # SSE run 事件缓冲区在 run 结束后的保留秒数 (断点续传窗口, 过期后前端降级为拉历史)。
    stream_buffer_ttl: int = 600
    # 单个 run 允许缓存的事件条数上限: 超限后只保留尾部并折叠一条提示。
    # 不设上限时一次长回答的 token 级事件(几千条)会一直堆在内存里,
    # 而断点续传只需要"尾部 + id 单调"这两个性质。
    stream_max_events: int = 4000
    # 同时在跑的流式 run 上限(背压闸): 超过即立刻 503, 而不是把下游(LLM 配额 /
    # PG 连接池 / 事件循环)拖到全体超时 —— 后者会让 1000 个人一起拿到坏结果。
    stream_max_concurrent_runs: int = 200
    # 已结束的 run 最多攒多少个缓冲区(内存硬顶): 触顶时按"结束时间最老"提前回收,
    # 与 stream_buffer_ttl 无关(被挡下的是"很多人刷新页面但都不再回来"的情况)。
    stream_max_buffers: int = 2000

    # ---------- 记忆层: Session Memory(Redis) / Working State(Checkpoint) ----------
    # 关闭或连接失败时一律静默降级 (内存 dict / InMemorySaver), 不阻断对话。
    redis_enabled: bool = True
    # 宿主轨默认地址 = compose 已发布的宿主端口(原 6379 落进 Windows winnat 排除段,
    # 已由 docker/.env 的 REDIS_HOST_PORT 抬到 16379)。容器内不由本默认值决定,
    # 走 compose 注入的 redis://redis:6379(服务名 + 容器内监听端口)。
    redis_url: str = "redis://localhost:16379/0"
    # Session Memory 滚动窗口 + 摘要的 Redis Key TTL (秒)
    session_memory_ttl: int = 3600
    # Redis 不可用时降级为进程内 dict 的会话数上限(LRU 淘汰)。不设上限时
    # 每个会话永久占一条且 turns 只增不减, 长时间降级 = 内存单调增长直到 OOM。
    session_memory_local_max: int = 5000
    # Working State Checkpointer 总开关: 关闭则退回进程内 InMemorySaver
    checkpoint_enabled: bool = True

    # ---------- 长期记忆: Vector(pgvector) + Graph(Neo4j) ----------
    # Vector 通道总开关: 关闭则不写/不查 long_term_memories, 也不做长期记忆提取。
    long_term_memory_enabled: bool = False
    # Graph 通道(Neo4j)总开关: 单节点内网部署, 对齐 ES 的"无认证"先例。
    graph_memory_enabled: bool = True
    # 代码默认值属宿主轨; bolt 原 7687 已落进本机 winnat 排除段(7630-7729),
    # 跟 compose 的 NEO4J_BOLT_HOST_PORT 一起抬到 17687 (容器内监听仍为 7687)。
    neo4j_uri: str = "bolt://localhost:17687"
    # Neo4j 侧 NEO4J_AUTH=none 时这两项留空即可; 若启用鉴权则填对应账号。
    neo4j_user: str = ""
    neo4j_password: str = Field(default="", repr=False)
    # 长期记忆语义查重的 cosine 相似度阈值: 新事实与既有记忆高于此值视为同一条,
    # 只刷新 last_accessed_at/content, 不重复插入。
    memory_dedup_threshold: float = 0.92
    # 每轮召回的长期记忆条数 (Vector Top-K) 与 Graph 关联事实跳数
    long_term_memory_top_k: int = 3
    graph_memory_hops: int = 2

    # ---------- 文档知识图谱 (GraphRAG, 复用 Neo4j, 与 :MemoryEntity 隔离) ----------
    # 总开关: 默认关闭, 关闭或 Neo4j 不可用时抽取/写入/查询全部静默降级(对齐
    # graph_memory_enabled 的降级风格), 不阻断入库与对话。
    doc_kg_enabled: bool = False
    # 单文档送入 LLM 抽取实体/关系的文本上限(控成本, 超长截断)
    kg_extraction_max_chars: int = 6000
    # 邻域展开默认跳数与单次返回节点上限(避免整图拉取, 控前端渲染)
    kg_graph_hops: int = 2
    kg_graph_node_limit: int = 300
    # 概览中度数低于此值的孤立实体节点被裁剪(降噪)
    kg_min_entity_degree: int = 1
    # 单篇文档落库的关系条数硬上限: 提示词里的"若干条"管不住模型, 超出的边
    # 会把图糊成一团(而且每条边都要过一次 MERGE), 以代码侧截断为准。
    kg_max_relations_per_doc: int = 40
    # 受控关系词表开关: 关掉即退回"关系词原样入库"的旧行为(仅作回退闸,
    # 词表本体在 app/kg/vocab.py, 不做成配置以免宿主/容器两轨各调一份)。
    kg_relation_vocab_enabled: bool = True

    # ---------- 个人级记忆: User Memory / Episodic / Personal Knowledge ----------
    # 总开关: 关闭则完全回退到旧的单一 fact 通道(仍受 long_term_memory_enabled 约束)。
    personal_memory_enabled: bool = True
    # 画像注入 prompt 的字符上限: 画像不检索、每轮全量带, 不设上限会随对话越滚越大。
    profile_max_chars: int = 400
    # 波动类属性(体重/身高/部门/职位等)每键保留的观测条数上限: 当前值按生效时间从
    # 这些观测里派生, 被顶掉的留在历史里给"我的记忆"页看; 封顶保证画像仍是一人一行、
    # 不长成第二张记忆表。
    profile_history_max_entries: int = 10
    # "还算当下生效"的宽限期: 用户明说了时间但只在此天数以内(如"从上个月起我改汇报给张总"),
    # 视为持续到当下的变更而不当历史处理; 超出宽限期才归入历史(只入历史不改当前值)。
    # 太小会把近期变更误判成陈迹(新值进不了当前态), 太大则十年前的陈述又能顶掉当前值。
    profile_current_grace_days: int = 90
    # 各桶每轮注入条数: 偏好/习惯是标量直读, 情节/知识是向量召回。
    memory_preference_top_k: int = 3
    memory_habit_top_k: int = 3
    memory_episode_top_k: int = 2
    memory_knowledge_top_k: int = 3
    # 情节召回时间窗: 只取近 N 天的事件, 陈年旧事不再挤占 prompt。
    episodic_window_days: int = 30
    # 已废弃: 情节 -> 知识蒸馏已下线(知识桶只由显式"记一下"写入)。键保留是为了
    # 宿主/容器两轨 .env 里的旧赋值不被 pydantic-settings 拒绝, 代码不再读它。
    memory_reflect_min_episodes: int = 3
    # 显式知识记录总开关: 用户消息命中"记一下"类指令词才写 knowledge 桶。
    memory_record_enabled: bool = True
    # 指令触发词(逗号分隔, 拼成正则只扫用户消息): 命中才调 LLM 提炼落库;
    # "以后都"这类易误触的说法默认不进词表, 需要时在 .env 追加。
    memory_record_keywords: str = "记一下,记住,帮我记,记下来,别忘了"
    # 情节陈旧判定天数: 超出后排序降权(仅影响排序, 不删数据)。
    memory_decay_days: int = 90
    # 语义查重命中后的"同一条观测"判定窗口: 两条 occurred_at 相差超过此天数就当同一事实
    # 的不同时刻观测, 各存一条而不是拿新文案覆盖旧记录(否则"2015 年 64kg"会抹掉"现在 70kg")。
    memory_observation_window_days: int = 7

    # ---------- 缓存层: Prompt Cache / Retrieval Cache / Tool Cache (统一落 Redis) ----------
    # 总开关: 关闭后全部直连真实调用(等同于本功能上线前的行为)。
    cache_enabled: bool = True
    prompt_cache_ttl: int = 300
    retrieval_cache_ttl: int = 300
    # Tool Cache 必须最短: 余额/审批进度等是实时数据, TTL 过长会读到脏结果。
    tool_cache_ttl: int = 30
    # Redis 连接池上限: redis.asyncio 默认 max_connections=50, 与百人并发的
    # "每条命令借还一次"叠加会直接触发 ConnectionError(连接池爆)。
    redis_max_connections: int = 100
    # ES 检索侧超时(秒)与稀疏通道并发上限。request_timeout 原为 30s —— ES 半死时
    # 每个查询都挂满 30s 把连接与内存拖爆; 这里压到秒级, 超时就按"稀疏通道为空"降级。
    es_search_timeout: float = 3.0
    es_search_concurrency: int = 32
    # BM25 全量重建(灌语料)侧的超时, 比检索宽松得多, 单独一档不要和检索混用。
    es_rebuild_timeout: int = 120
    # Ollama embedding: 查询向量在对话热路径上, 批量向量在入库路径上, 两者共用一个
    # 进程级连接池; 上限要按"Ollama 单进程串行推理"来给, 太大只会把排队搬到下游。
    embedding_timeout: float = 60.0
    embedding_query_timeout: float = 10.0
    embedding_max_connections: int = 32
    # 查询向量的并发上限(在本地排队, 而不是把 Ollama 压到报错):
    # Ollama 多请求并发时会在内部 tokenize 阶段失败返 400(实测 30 并发必出现),
    # 表现是"意图语义层集体下沉 LLM + 检索稠密通道集体为空"—— 看着像降级正常, 实则
    # 质变。取 8: 单模型串行推理下再多的并发也只会在 Ollama 内部排队。
    embedding_query_concurrency: int = 8
    # MCP 工具清单缓存 TTL(秒): 每次 tool_call 都重新 discover 会新建一条 streamable-http
    # 会话, 百人并发时 MCP server 会被 discover 打满; TTL 到期才重新发现。
    mcp_tools_ttl: int = 300
    # A2A 委派超时与连接池: 委派是"多步办理", 天然比一次 LLM 调用慢, 给足但必须有上限。
    a2a_timeout: float = 120.0
    a2a_connect_timeout: float = 10.0
    a2a_max_connections: int = 64
    a2a_max_keepalive: int = 16

    # MCP servers
    # 默认值 = Docker 已发布的宿主端口 (Windows winnat 把 7956-8055 列进 TCP 排除段,
    # 8001/8002 无法 bind; 详见 docker-compose.yml 顶部"宿主端口映射约定")。
    # 容器内由 compose 注入 http://<svc>:8001/mcp 等服务名地址覆盖本默认值。
    hr_mcp_url: str = "http://localhost:18001/mcp"
    finance_mcp_url: str = "http://localhost:18002/mcp"
    # 数据洞察与采购合同两个新域: 宿主端口同样走 18xxx 约定(容器内监听 8005/8006)。
    analytics_mcp_url: str = "http://localhost:18005/mcp"
    procurement_mcp_url: str = "http://localhost:18006/mcp"

    # A2A agents
    # 9001/9002 不在历史排除段内, 故容器与宿主同端口; 新增两个专业智能体沿用该约定。
    hr_agent_url: str = "http://localhost:9001"
    finance_agent_url: str = "http://localhost:9002"
    analyst_agent_url: str = "http://localhost:9005"
    contract_agent_url: str = "http://localhost:9006"

    # ---------- 进程内 web 工具 (app/tools/web.py: 联网检索与抓取) ----------
    # 检索 provider: ddgs 免密默认; tavily/serper 需密钥(见 docker/secrets/)。provider
    # 不可用或无密钥时自动回退 ddgs; 全部失败返回 {error, results: []} 而不是抛出。
    web_search_provider: str = "ddgs"  # ddgs | tavily | serper
    web_search_max_results: int = 5
    web_search_timeout: float = 10.0
    # 检索结果比业务实时数据稳定, Tool Cache TTL 可放宽(默认 30s 是给余额/进度类工具的)。
    web_search_cache_ttl: int = 300
    # 检索出口代理(可选, 仅作用于 search_web 的 ddgs/tavily/serper, 不作用于 fetch_url)。
    # ddgs 抓的是 Google/DuckDuckGo/Brave/Yahoo 等端点, 在大陆网络直连不可达(全部超时),
    # 需要一个能翻出去的 HTTP(S) 代理。留空 = 直连(不受限网络/开发机默认)。容器内填宿主
    # 代理地址, Docker Desktop 用 http://host.docker.internal:<port>(需代理开启 Allow LAN)。
    # 刻意不代理 fetch_url: 其 SSRF 护栏按容器直连视角解析 DNS, 走代理会让出口与校验视角
    # 不一致而削弱护栏, 故抓取保持直连(单条结果抓不到按其契约降级, 不影响检索)。
    web_search_proxy: str = ""
    # ddgs 尝试的引擎顺序(逗号分隔): 逐个独立尝试, 单引擎超时/空结果自动换下一个。ddgs 的 "auto"
    # 会含极慢的 google/bing 与常空结果的 wikipedia/grokipedia/startpage, 走代理时易拖垒整体;
    # duckduckgo/yahoo/brave 三者全球可达且快(典型首个 1.5~3s 命中)。置 "auto" 可恢复 ddgs 全量兜底。
    web_search_ddgs_backends: str = "duckduckgo,yahoo,brave"
    # 检索密钥(可选): 默认 ddgs 免密, 两者都留空即可; 密钥只住 docker/secrets/*.txt。
    tavily_api_key: str = Field(default="", repr=False)
    serper_api_key: str = Field(default="", repr=False)
    # 抓取护栏: 逗号分隔的域名白名单(空 = 不启用白名单, 仅靠 SSRF 拒内网);
    # 超时/体积/正文字符上限/重定向上限见下。所有抓取都过 url_guard, 私网/元数据地址一律拒。
    web_fetch_allowlist: str = ""
    web_fetch_timeout: float = 15.0
    web_fetch_max_bytes: int = 2_000_000
    web_fetch_max_chars: int = 8000
    web_fetch_max_redirects: int = 3

    # ---------- 进程内 docgen 工具 (app/tools/docgen.py: Word/Excel/PPT/PDF/MD/图片 生成) ----------
    # 单个生成物体积上限(失控 spec 的刹车), 产物落 upload_dir/gen/<token>/;
    # retention 到期由生成时顺带清扫(opportunistic), 不依赖定时任务。
    docgen_max_bytes: int = 20_000_000
    docgen_retention_hours: int = 24
    # 下载链接前缀: 空则工具返回相对路径 /api/files/<token>/<file>(同源 SPA 内可直接点);
    # 需要跨域分发(如把链接发给外网同事)时才配置绝对地址。
    public_base_url: str = ""
    # 文档内嵌图片(spec.images)治理: 单张原图字节上限 + 归一化后最长边像素。
    # 过大图既撑爆生成物体积也让下载变慢; 统一转 PNG 后各 builder 只读本地文件, 网络/SSRF
    # 留在异步解析层(app/docgen/images.py), 不污染纯同步的 builder。
    docgen_image_max_bytes: int = 8_000_000
    docgen_image_max_edge: int = 1600

    # ---------- 能力域闸门 (web 联网检索 / docgen 文件生成, 见 app/security/quota.py) ----------
    # 这两个域的工具在本进程内, 不进 MCP 角色×工具矩阵 —— 原先既没有域级闸门也没有
    # 用量上限, 等于给任意调用者一个可以批量消耗 LLM 配额与磁盘的写通道。现在两件事
    # 都补上: 哪些角色能用(白名单) + 每人每天能用多少次(计数限流)。
    # 默认全员自助(与原行为一致, 只是加了上限); 收紧只需改配置不必改代码。
    capability_allowed_roles: str = "employee,manager,hr,finance,admin"
    # 同一调用者在同一能力域上的自然日调用上限(一次 tool_call 路由计 1 次)。
    capability_daily_limit: int = 60

    # Security
    audit_log_path: str = "./logs/audit.jsonl"

    # LangSmith / LangGraph Studio (仅本地开发环境启用; 生产容器必须关闭)
    # 开启后 trace 会上传到 langsmith_endpoint 指向的服务, 含对话内容,
    # 受内网合规约束: 默认 false, 只有开发机在 .env 里显式置 LANGSMITH_TRACING=true 才启用
    # (容器侧由 docker/.env 的 LANGSMITH_TRACING=false 锁死)。
    # 注意: 这是 pydantic 字段"默认值", 优先级低于 .env/环境变量 —— 想开 trace 改 .env,
    langsmith_tracing: bool = False
    langsmith_api_key: str = Field(default="", repr=False)
    langsmith_project: str = "mxi-assistant"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # Langfuse (自托管 LLM 可观测, 与 LangSmith 并存的独立开关)
    # 与 LangSmith 的差别: 上报目标是同 compose 网络内自建的 langfuse-web
    # (见 docker-compose 的 profile="langfuse" 栈), 对话数据不出容器网络,
    # 容器侧允许开启; 但默认仍 false, 由 docker/.env 置 LANGFUSE_ENABLED=true 打开。
    # 密钥: docker/secrets/langfuse_api_key.txt 单文件两行 = secret key(第一行)
    # / public key(第二行), 加载完成后在 model_post_init 拆分;
    # dotenv 里 LANGFUSE_API_KEY / LANGFUSE_PUBLIC_KEY 留空(红线: 密钥不进 dotenv)。
    langfuse_enabled: bool = False
    langfuse_api_key: str = Field(default="", repr=False)
    langfuse_public_key: str = Field(default="", repr=False)
    # 容器内用服务名 langfuse-web:3000; 宿主轨默认指向已发布端口 18100。
    langfuse_base_url: str = "http://localhost:18100"
    # Langfuse 侧靠 pk/sk 区分项目, 项目名不参与路由; 本字段只用作 trace 名
    # (metadata 的 langfuse_trace_name), 便于在 UI 里跟其他接入方分开。
    langfuse_project: str = "mxi-assistant"
    # 映射 LANGFUSE_TRACING_ENVIRONMENT: trace 按部署环境区分(development/staging/...)
    langfuse_environment: str = "development"

    @field_validator(
        "pg_password", "deepseek_api_key", "zhipu_api_key", "langsmith_api_key",
        "langfuse_api_key", "mongo_password", "tavily_api_key", "serper_api_key",
        mode="after",
    )
    @classmethod
    def _read_from_secret_file(cls, value: str, info) -> str:
        """环境变量/.env 未提供时, 回退读取 compose secret 文件。

        候选路径按序: 容器内 /run/secrets/<name> -> 宿主机 docker/secrets/<name>.txt
        (本地直跑无挂载路径时的开发级回退)。
        """
        if value:
            return value
        paths = [Path(_SECRET_FILES[info.field_name])]
        host_rel = _SECRET_HOST_FALLBACK.get(info.field_name)
        if host_rel:
            paths.append(Path(__file__).resolve().parent.parent / host_rel)
        for p in paths:
            if p.is_file():
                raw = p.read_text(encoding="utf-8").strip()
                # secret 文件里的占位值(如未填的 zhipu_api_key.txt)视同未配置,
                # 让调用侧报"缺少密钥"而不是带占位值去请求被 401
                if raw.upper().startswith("REPLACE_ME"):
                    return value
                return raw
        return value

    @property
    def llm_providers(self) -> dict[str, dict]:
        """解析 llm_providers_json; 非法 JSON 时回退代码默认注册表(不阻断启动)。"""
        try:
            parsed = json.loads(self.llm_providers_json)
        except (ValueError, TypeError):
            parsed = _DEFAULT_LLM_PROVIDERS
        if not isinstance(parsed, dict):
            parsed = _DEFAULT_LLM_PROVIDERS
        return {str(k).lower(): v for k, v in parsed.items() if isinstance(v, dict)}

    def resolve_llm_provider(self, model: str) -> tuple[str, dict] | None:
        """按模型名匹配在线供应商(最长前缀优先); 未命中 = None -> 走本地 Ollama。"""
        name = (model or "").lower()
        best: tuple[str, dict] | None = None
        for prefix, cfg in self.llm_providers.items():
            if prefix and name.startswith(prefix):
                if best is None or len(prefix) > len(best[0]):
                    best = (prefix, cfg)
        return best

    def model_post_init(self, __context) -> None:
        """全部字段填完后拆 langfuse 双密钥: secret 文件单文件两行(sk/pk)。

        不能放 field_validator 里做: 另一个 mode="after" 校验器随后会把整份
        文件内容盖回已拆分的值; post_init 在所有校验器之后跑, 只改一次。
        仅在 LANGFUSE_PUBLIC_KEY 环境变量未显式提供时回填 pk。
        """
        lines = [ln.strip() for ln in self.langfuse_api_key.splitlines() if ln.strip()]
        if len(lines) >= 2:
            object.__setattr__(self, "langfuse_api_key", lines[0])
            if not self.langfuse_public_key:
                object.__setattr__(self, "langfuse_public_key", lines[1])

    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
