"""Synchronous MySQL access for MCP servers.

FastMCP tools run as plain sync functions, so the MCP servers use a
synchronous SQLAlchemy engine (pymysql) instead of the async engine in
app.db.session. Connection settings come from the same Settings source
(.env / docker/.env / 环境变量 / /run/secrets/mysql_password).
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
    password = get_settings().mysql_password
    if not password:
        raise RuntimeError(
            "缺少 MySQL 密码: 请设置环境变量 MYSQL_PASSWORD, "
            "或在项目根目录 .env / docker/.env 中添加 MYSQL_PASSWORD=..., "
            "Docker 部署则创建 docker/secrets/mysql_password.txt"
            "(容器内挂载为 /run/secrets/mysql_password)"
        )
    return password


def get_sync_engine() -> Engine:
    """Lazily create the shared synchronous engine (pymysql driver)."""
    global _engine
    if _engine is None:
        s = get_settings()
        url = URL.create(
            "mysql+pymysql",
            username=s.mysql_user,
            password=_ensure_password(),
            host=s.mysql_host,
            port=s.mysql_port,
            database=s.mysql_database,
            query={"charset": "utf8mb4"},
        )
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args={"connect_timeout": s.mysql_connect_timeout},
        )
    return _engine


def execute_readonly_sql(sql: str, allowed_tables: set[str]) -> list[dict]:
    """安全执行 Text2SQL 生成的只读 SELECT, 返回行字典列表。

    统一 json 化处理 Decimal / datetime, 便于 MCP 工具直接返回。
    """
    from app.db.sql_guard import inject_timeout_hint, validate_readonly_select

    safe_sql = validate_readonly_select(sql, allowed_tables)
    safe_sql = inject_timeout_hint(safe_sql)
    engine = get_sync_engine()
    with engine.connect() as conn:
        result = conn.execute(text(safe_sql))
        cols = list(result.keys())
        rows = []
        for row in result.mappings():
            rows.append({k: _jsonify(v) for k, v in row.items()})
        return [{"columns": cols, "rows": rows, "rowcount": len(rows)}]
