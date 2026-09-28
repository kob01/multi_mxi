"""Synchronous PostgreSQL access for MCP servers.

FastMCP tools run as plain sync functions, so the MCP servers use a
synchronous SQLAlchemy engine (psycopg3) instead of the async engine in
app.db.session. Connection settings come from the same Settings source
(宿主 .env / .env.local, 容器侧 compose env_file+environment, 环境变量, 以及
/run/secrets/pg_password 回退)。
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Engine

from app.config import get_settings

_engine: Engine | None = None


def _jsonify(value):
    """把 DB 返回值转成 JSON 可序列化类型。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime.datetime) else value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def _ensure_password() -> str:
    password = get_settings().pg_password
    if not password:
        raise RuntimeError(
            "缺少 PostgreSQL 密码，请设置环境变量 PG_PASSWORD"
        )
    return password


def get_sync_engine() -> Engine:
    """Lazily create the shared synchronous engine (psycopg3 driver)."""
    global _engine
    if _engine is None:
        s = get_settings()
        url: str | URL
        if s.database_url:
            # 同一个 DATABASE_URL 在同步侧换成 psycopg 驱动 (异步引擎用 asyncpg)。
            url = s.database_url.replace("postgresql+asyncpg", "postgresql+psycopg")
        else:
            url = URL.create(
                "postgresql+psycopg",
                username=s.pg_user,
                password=_ensure_password(),
                host=s.pg_host,
                port=s.pg_port,
                database=s.pg_database,
            )
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            # psycopg 走 libpq, 这些均为原生 libpq 参数 (无需像 asyncpg 那样转 SSLContext)。
            connect_args={
                "connect_timeout": s.pg_connect_timeout,
                "sslmode": s.pg_sslmode,
                "application_name": "mxi-mcp",
            },
        )
    return _engine


def execute_readonly_sql(
    sql: str, allowed_tables: set[str], params: dict | None = None
) -> list[dict]:
    """安全执行 Text2SQL 生成的只读 SELECT, 返回行字典列表。

    统一 json 化处理 Decimal / datetime, 便于 MCP 工具直接返回。

    ``params`` 只服务于"服务端自己拼的指标 SQL"(见 app/analytics/reports.py):
    走绑定参数而不是字符串内插, 既免转义又防注入。LLM 生成的 Text2SQL 一律
    不传 params —— 它已经被 sql_guard 按整条语句校验过, 加参数通道只会多一个
    绕过白名单的面。
    """
    from app.db.sql_guard import validate_readonly_select

    safe_sql = validate_readonly_select(sql, allowed_tables)
    engine = get_sync_engine()
    with engine.connect() as conn:
        # PostgreSQL 没有 MySQL 的 MAX_EXECUTION_TIME hint, 改用事务级
        # statement_timeout: SET LOCAL 只在当前(隐式)事务内生效, 不污染连接池。
        timeout_ms = int(get_settings().pg_statement_timeout_ms)
        conn.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
        result = conn.execute(text(safe_sql), params or {})
        cols = list(result.keys())
        rows = []
        for row in result.mappings():
            rows.append({k: _jsonify(v) for k, v in row.items()})
        conn.rollback()  # 只读: 立即结束事务, 释放超时设置与快照
        return [{"columns": cols, "rows": rows, "rowcount": len(rows)}]
