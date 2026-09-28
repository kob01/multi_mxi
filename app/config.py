"""Application settings loaded from environment / .env file."""

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
    "deepseek_api_key": "/run/secrets/deepseek_api_key",
    "mongo_password": "/run/secrets/mongo_password",
    "tavily_api_key": "/run/secrets/tavily_api_key",
    "serper_api_key": "/run/secrets/serper_api_key",
}
_SECRET_HOST_FALLBACK = {
    "pg_password": "docker/secrets/pg_password.txt",
    "langsmith_api_key": "docker/secrets/langsmith_api_key.txt",
    "deepseek_api_key": "docker/secrets/deepseek_api_key.txt",
    "mongo_password": "docker/secrets/mongo_password.txt",
    "tavily_api_key": "docker/secrets/tavily_api_key.txt",
    "serper_api_key": "docker/secrets/serper_api_key.txt",
}


class Settings(BaseSettings):
    """Central configuration for the whole platform.

    取值优先级(高->低): 真实环境变量 -> .env.local -> .env -> 字段默认值。

    配置只有一条宿主轨: ``.env`` 是**宿主机视角**(全部指向 docker 已发布端口),
    ``.env.local`` 是同一视角的机器私有覆盖(均已 gitignore)。**不要**把
    ``docker/.env`` 加进 env_file: pydantic-settings 是后者覆盖前者, 那会把容器
    服务名(tei-rerank/elasticsearch/mongo/...)灌进宿主机进程, 解析失败后静默降级
    (见 CONFIG_RULES.md 第 5 条)。容器侧一律靠 docker-compose 的
    ``env_file: docker/.env`` + ``environment:`` 注入真实环境变量。
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
    llm_model: str = "deepseek-flash"
    intent_model: str = "deepseek-flash"

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

    # RAG (向量块与业务元数据同库: PostgreSQL + pgvector, 见 knowledge_chunks 表)
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
    # 创作产物(图表 SVG / 周期报告 Markdown / 网页 HTML)的落盘目录; 容器侧指向持久卷 /data/reports
    # (与 UPLOAD_DIR 同源, 否则写在容器工作目录重启即丢)。
    report_dir: str = "./data/reports"
    # MinerU OCR 服务 (mineru-api, 用于图片解析; 需先启动 mineru-api 服务)
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

    # ---------- 记忆层: Session Memory(Redis) / Working State(Checkpoint) ----------
    # 关闭或连接失败时一律静默降级 (内存 dict / InMemorySaver), 不阻断对话。
    redis_enabled: bool = True
    redis_url: str = "redis://localhost:6379/0"
    # Session Memory 滚动窗口 + 摘要的 Redis Key TTL (秒)
    session_memory_ttl: int = 3600
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

    # ---------- 个人级记忆: User Memory / Episodic / Personal Knowledge ----------
    # 总开关: 关闭则完全回退到旧的单一 fact 通道(仍受 long_term_memory_enabled 约束)。
    personal_memory_enabled: bool = True
    # 画像注入 prompt 的字符上限: 画像不检索、每轮全量带, 不设上限会随对话越滚越大。
    profile_max_chars: int = 400
    # 各桶每轮注入条数: 偏好/习惯是标量直读, 情节/知识是向量召回。
    memory_preference_top_k: int = 3
    memory_habit_top_k: int = 3
    memory_episode_top_k: int = 2
    memory_knowledge_top_k: int = 3
    # 情节召回时间窗: 只取近 N 天的事件, 陈年旧事不再挤占 prompt。
    episodic_window_days: int = 30
    # 情节 -> 知识蒸馏门槛: 自上次蒸馏以来新增情节达到该条数才调一次 LLM。
    memory_reflect_min_episodes: int = 3
    # 情节陈旧判定天数: 超出后排序降权(仅影响排序, 不删数据)。
    memory_decay_days: int = 90

    # ---------- 缓存层: Prompt Cache / Retrieval Cache / Tool Cache (统一落 Redis) ----------
    # 总开关: 关闭后全部直连真实调用(等同于本功能上线前的行为)。
    cache_enabled: bool = True
    prompt_cache_ttl: int = 300
    retrieval_cache_ttl: int = 300
    # Tool Cache 必须最短: 余额/审批进度等是实时数据, TTL 过长会读到脏结果。
    tool_cache_ttl: int = 30

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

    @field_validator(
        "pg_password", "deepseek_api_key", "langsmith_api_key", "mongo_password",
        "tavily_api_key", "serper_api_key",
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
                return p.read_text(encoding="utf-8").strip()
        return value

    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
