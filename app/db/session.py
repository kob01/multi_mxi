"""PostgreSQL async engine / session factory.

单一存储层: 业务/文档元数据与 pgvector 知识块共用这一套连接, MCP server 的
同步访问在 app/db/sync.py (psycopg3), 二者读同一份 Settings。

Password policy: the database password is injected via the PG_PASSWORD
environment variable (or the .env file picked up by pydantic-settings).
It is never hard-coded and .env files are git-ignored.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _password() -> str:
    """Read the database password from the environment (PG_PASSWORD)."""
    password = get_settings().pg_password
    if not password:
        raise RuntimeError(
            "缺少 PostgreSQL 密码: 请设置环境变量 PG_PASSWORD, "
            "或在项目根目录 .env / docker/.env 中添加 PG_PASSWORD=..., "
            "Docker 部署则创建 docker/secrets/pg_password.txt"
            "(容器内挂载为 /run/secrets/pg_password)"
        )
    return password


def async_database_url() -> str:
    """Resolve the asyncpg DSN from DATABASE_URL or the discrete PG_* fields."""
    settings = get_settings()
    if settings.database_url:
        return settings.database_url
    # 密码可能含 @ / : 等 DSN 保留字符, 必须转义后再拼接。
    return (
        f"postgresql+asyncpg://{settings.pg_user}:{quote_plus(_password())}"
        f"@{settings.pg_host}:{settings.pg_port}/{settings.pg_database}"
    )


def _connect_args() -> dict:
    """asyncpg does not understand libpq's ``sslmode``; pass an SSLContext."""
    settings = get_settings()
    args: dict = {
        "timeout": settings.pg_connect_timeout,
        "server_settings": {"application_name": "mxi-assistant"},
    }
    if settings.pg_sslmode != "disable":
        import ssl

        ctx = ssl.create_default_context()
        if settings.pg_sslmode == "require":
            # 自签证书云实例: 只要求链路加密, 不校验 CA / 主机名。
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        args["ssl"] = ctx
    return args


def get_engine() -> AsyncEngine:
    """Lazily create the async engine (reads PG_PASSWORD on first use)."""
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(
            async_database_url(),
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args=_connect_args(),
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory (creates the engine on first call)."""
    get_engine()
    assert _session_factory is not None
    return _session_factory


# documents 表 ACL 列的轻量迁移 (create_all 不会给已存在的表加列)。
_DOC_ACL_COLUMNS = {
    "visibility": "VARCHAR(16) NOT NULL DEFAULT 'public'",
    "owner_id": "VARCHAR(64) NOT NULL DEFAULT ''",
    "dept_id": "VARCHAR(64) NOT NULL DEFAULT ''",
    "allowed_roles": "VARCHAR(128) NOT NULL DEFAULT ''",
}


def _documents_columns(sync_conn) -> set[str]:
    """Existing column names of ``documents`` via the dialect inspector (no raw SQL)."""
    return {c["name"] for c in inspect(sync_conn).get_columns("documents")}


async def init_schema() -> None:
    """Create all metadata tables if they do not exist yet.

    pgvector 扩展必须先行: ``knowledge_chunks.embedding`` 编译成 DDL 时需要
    ``vector`` 类型已在 search_path 中。容器由 docker/init/01_vector.sql 以超管
    预建, 这里再兜底一次 —— 失败时抛明确错误, 而不是让 create_all 报难懂的
    "type vector does not exist"。

    Also backfills document-ACL columns on a pre-existing ``documents`` table
    (``create_all`` never ALTERs existing tables), so upgrading an old database
    stays safe and idempotent.
    """
    from app.db.models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        try:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception as exc:
            raise RuntimeError(
                "知识库需要 pgvector 扩展 (CREATE EXTENSION vector), 当前账号无权限创建: "
                f"{exc}; 请让超级用户执行一次 CREATE EXTENSION IF NOT EXISTS vector"
            ) from exc
        await conn.run_sync(Base.metadata.create_all)
        have = await conn.run_sync(_documents_columns)
        for name, ddl in _DOC_ACL_COLUMNS.items():
            if name not in have:
                await conn.execute(text(f"ALTER TABLE documents ADD COLUMN {name} {ddl}"))


def db_available() -> bool:
    """Whether the engine has been initialised (password already provided)."""
    return _engine is not None
