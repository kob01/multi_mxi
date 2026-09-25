"""Application settings loaded from environment / .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 敏感项对应的 Docker secret 文件(BuildKit/compose secrets 挂载路径)
_SECRET_FILES = {
    "pg_password": "/run/secrets/pg_password",
    "langsmith_api_key": "/run/secrets/langsmith_api_key",
    "deepseek_api_key": "/run/secrets/deepseek_api_key",
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
        """环境变量/.env 未提供时, 回退读取 compose secret 文件。"""
        if value:
            return value
        secret_path = Path(_SECRET_FILES[info.field_name])
        if secret_path.is_file():
            return secret_path.read_text(encoding="utf-8").strip()
        return value

    @property
    def base_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
