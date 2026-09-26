"""Application settings loaded from environment / .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 敏感项对应的 Docker secret 文件(BuildKit/compose secrets 挂载路径);
# 本地宿主机直跑时回退读取项目内 docker/secrets/<name>.txt (开发调试用)
_SECRET_FILES = {
    "pg_password": "/run/secrets/pg_password",
    "langsmith_api_key": "/run/secrets/langsmith_api_key",
    "deepseek_api_key": "/run/secrets/deepseek_api_key",
}
_SECRET_HOST_FALLBACK = {
    "pg_password": "docker/secrets/pg_password.txt",
    "langsmith_api_key": "docker/secrets/langsmith_api_key.txt",
    "deepseek_api_key": "docker/secrets/deepseek_api_key.txt",
}


class Settings(BaseSettings):
    """Central configuration for the whole platform.

    Real environment variables take precedence over the .env files
    (.env and docker/.env are both supported for local convenience).
    """

    model_config = SettingsConfigDict(
        env_file=(".env", "docker/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # 字段取默认值(环境变量缺失)时也运行校验器, 以便回退读取 /run/secrets/*
        validate_default=True,
    )

    # Ollama (仅 embedding / rerank 仍走本地 Ollama)
    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = "bge-m3"
    rerank_model: str = "dengcao/bge-reranker-v2-m3"

    # DeepSeek 在线 API (OpenAI 兼容格式, 用于 LLM / 意图识别)
    deepseek_base_url: str = "https://api.deepseek.com"
    # 密钥优先取环境变量, 其次由校验器读取 /run/secrets/deepseek_api_key
    deepseek_api_key: str = ""
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
    # Rerank fails -> graceful fallback to RRF fusion order (e.g. Windows
    # Ollama llama.cpp crashes on bge-reranker GGUF); set false to skip.
    rerank_enabled: bool = True
    # 全链路唯一的相关性阈值, 只作用于 rerank 阶段: rerank 分低于该值的块
    # 视为噪声剔除; 剔除后为空即"未检索到相关文档", 触发改写重检或拒答。
    # 检索通道(稠密向量 / ES BM25)与 RRF 融合均不设阈值, 只负责召回。
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
    pg_password: str = ""
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

    # Document upload & metadata
    upload_dir: str = "./data/uploads"
    upload_max_mb: int = 50
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
    long_term_memory_enabled: bool = True
    # Graph 通道(Neo4j)总开关: 单节点内网部署, 对齐 ES 的"无认证"先例。
    graph_memory_enabled: bool = True
    neo4j_uri: str = "bolt://localhost:7687"
    # Neo4j 侧 NEO4J_AUTH=none 时这两项留空即可; 若启用鉴权则填对应账号。
    neo4j_user: str = ""
    neo4j_password: str = ""
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
    hr_mcp_url: str = "http://localhost:8001/mcp"
    finance_mcp_url: str = "http://localhost:8002/mcp"

    # A2A agents
    hr_agent_url: str = "http://localhost:9001"
    finance_agent_url: str = "http://localhost:9002"

    # Security
    audit_log_path: str = "./logs/audit.jsonl"

    # LangSmith / LangGraph Studio (仅本地开发环境启用; 生产容器默认关闭)
    # 开启后 trace 会上传到 langsmith_endpoint 指向的服务, 含对话内容,
    # 受内网合规约束: 生产环境务必保持 LANGSMITH_TRACING=false。
    langsmith_tracing: str = "false"
    langsmith_api_key: str = ""
    langsmith_project: str = "mxi-assistant"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    @field_validator("pg_password", "deepseek_api_key", "langsmith_api_key", mode="after")
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
