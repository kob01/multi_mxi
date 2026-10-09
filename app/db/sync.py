"""Synchronous PostgreSQL access for MCP servers.

FastMCP tools run as plain sync functions, so the MCP servers use a
synchronous SQLAlchemy engine (psycopg3) instead of the async engine in
app.db.session. Connection settings come from the same Settings source
(宿主 .env / .env.local, 容器侧 compose env_file+environment, 环境变量, 以及
/run/secrets/pg_password 回退)。

两个安全接入点(层 0/1):
- ``db_role``: 连接借出时 ``SET ROLE`` 到 NOLOGIN 最小权限角色, 归还时 ``RESET ROLE``;
  登录身份仍然是表属主(要能跑 DDL/其余业务), 但每条语句的**有效权限**只剩角色
  被授过的那一份。属主默认绕过 RLS, 只有切到非超级用户角色后策略才真生效。
- ``session_settings``: 以 ``set_config(..., is_local=true)`` 写进当前事务的会话变量
  (如 ``app.tenant_id`` / ``app.dept_scope``), 供 RLS 策略与域谓词读取。用 LOCAL 而非
  会话级 SET: 事务结束自动消失, 不会污染连接池里的下一个借用人。
"""

from __future__ import annotations

import datetime
import logging
import re
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import URL, create_engine, event, text
from sqlalchemy.engine import Engine

from app.config import get_settings

logger = logging.getLogger(__name__)

# 按生效角色分池: None = 不切角色(现状), 其余为配置里的 NOLOGIN 角色名。
_engines: dict[str | None, Engine] = {}

# 角色名来自配置(不是用户输入), 但仍按标识符白名单校验后再拼: 配置被污染时
# 最多是连不上, 不能变成一条任意 SQL。
_ROLE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _validated_role(db_role: str | None) -> str | None:
    """角色名白名单校验; 不合法时退回"不切角色"并告警(而不是抛出打断取数)。"""
    if not db_role:
        return None
    if not _ROLE_IDENT_RE.match(db_role):
        logger.warning("非法的 PG 角色名 %r, 本次不切角色", db_role)
        return None
    return db_role


def _attach_role_events(engine: Engine, db_role: str) -> None:
    """在 checkout/checkin 上挂 SET ROLE / RESET ROLE。

    必须挂在 checkout 而不是建连接时: 连接池会复用物理连接, 一次性 SET ROLE 会被
    下一个借用人继承(越权), 而每次借出都重设、归还都复位才是闭合的。

    两处都要跟一个 commit: psycopg3 会为任何语句开隐式事务, 而带着 INTRANS 状态回到
    SQLAlchemy 的连接在池子切换 autocommit 时直接报错(表现为"每次取数都连不上")。
    ``SET ROLE`` 不带 LOCAL, 提交后仍然生效, 所以两者不矛盾。
    另外: 连接在 checkout 失败时会被以 None 传回 checkin, 所以处理函数必须能接住 None
    与异常 —— 否则一个无关的 AttributeError 会把真正的错盖掉。
    """

    def _on_checkout(dbapi_connection, _record, _args) -> None:  # noqa: ANN001
        if dbapi_connection is None:
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f'SET ROLE "{db_role}"')
        finally:
            cursor.close()
        dbapi_connection.commit()

    def _on_checkin(dbapi_connection, _record) -> None:  # noqa: ANN001
        if dbapi_connection is None:
            return
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("RESET ROLE")
            cursor.close()
            dbapi_connection.commit()
        except Exception:  # noqa: BLE001 - 归还路径不得掩盖主错
            pass

    event.listen(engine, "checkout", _on_checkout)
    event.listen(engine, "checkin", _on_checkin)


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


def get_sync_engine(db_role: str | None = None) -> Engine:
    """Lazily create the shared synchronous engines (psycopg3 driver), keyed by role."""
    role = _validated_role(db_role)
    engine = _engines.get(role)
    if engine is not None:
        return engine
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
    if role and role in {s.pg_role_analytics_read, s.pg_role_analytics_write} - {""}:
        # analytics 专用角色走小池: 那是"独立资源隔离"在本仓的等效手段(PG 无原生
        # resource group), 防止分析/写通道把业务主库的连接预算打满。
        pool_size, max_overflow = s.pg_analytics_pool_size, 0
    else:
        pool_size, max_overflow = s.pg_sync_pool_size, s.pg_sync_max_overflow
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        # 同步池容量显式给: 默认 5+10 在"每个业务工具调用都拿一条"的 ReAct 循环
        # 下会排队(抢不到默认等 30s 再抛), 而同步工具是在线等结果的路径。
        # 本引擎不只服务网关: 每个 mcp/agent 进程也各自建一份, 所以取小值
        # (它是总预算里的乘数项, 对账口径见 Settings.pg_pool_size 注释)。
        pool_size=max(1, pool_size),
        max_overflow=max(0, max_overflow),
        pool_timeout=max(1, s.pg_pool_timeout),
        # psycopg 走 libpq, 这些均为原生 libpq 参数 (无需像 asyncpg 那样转 SSLContext)。
        connect_args={
            "connect_timeout": s.pg_connect_timeout,
            "sslmode": s.pg_sslmode,
            "application_name": "mxi-mcp",
        },
    )
    if role:
        _attach_role_events(engine, role)
    _engines[role] = engine
    return engine


def analytics_read_role() -> str | None:
    """analytics 取数应使用的只读角色名(配置为空时退回属主连接, 与改造前一致)。"""
    return _validated_role(get_settings().pg_role_analytics_read or None)


def analytics_write_role() -> str | None:
    """analytics 写通道应使用的角色名(只被授过白名单表的 UPDATE/INSERT)。"""
    return _validated_role(get_settings().pg_role_analytics_write or None)


def _apply_session_settings(
    conn, session_settings: dict[str, str] | None, timeout_ms: int
) -> None:
    """把语句超时与会话作用域变量写进**当前事务**。

    用 ``set_config(name, value, true)`` 而不是 ``SET LOCAL name = ...``: 前者能走绑定
    参数, 值永不进 SQL 文本(层 5-A 的口径对服务端自己也要成立); 第三参 true =
    transaction-local, 事务结束自动失效, 连接池复用不会把上一个调用者的部门带过去。

    占位符用 SQLAlchemy 的 ``:name`` 形式(与全仓其它 ``text()`` 一致): 写成 DBAPI 的
    ``%(name)s`` 不会被 text() 识别, 会被原样递给驱动而报语法错。
    """
    conn.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": str(int(timeout_ms))},
    )
    for key, value in (session_settings or {}).items():
        conn.execute(
            text("SELECT set_config(:key, :value, true)"),
            {"key": key, "value": str(value)},
        )


@contextmanager
def scoped_connection(
    db_role: str | None = None,
    session_settings: dict[str, str] | None = None,
    timeout_ms: int | None = None,
):
    """在"生效角色 + 会话作用域 + 语句超时"三重约束下开一个事务级连接。

    正常退出提交、异常回滚(层 4-5 的事务包裹与超时是写护栏的地基)。只读路径也走
    这里: 事务内 ``set_config(..., true)`` 与 RLS 策略的读取窗口必须重合, 否则策略
    会在下一个语句里看不到作用域变量而把合法查询全拒。
    """
    settings = get_settings()
    limit = int(timeout_ms if timeout_ms is not None else settings.pg_statement_timeout_ms)
    engine = get_sync_engine(db_role)
    with engine.connect() as conn:
        _apply_session_settings(conn, session_settings, limit)
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()


def execute_readonly_sql(
    sql: str,
    allowed_tables: set[str],
    params: dict | None = None,
    *,
    db_role: str | None = None,
    session_settings: dict[str, str] | None = None,
) -> list[dict]:
    """安全执行 Text2SQL 生成的只读 SELECT, 返回行字典列表。

    统一 json 化处理 Decimal / datetime, 便于 MCP 工具直接返回。

    ``params`` 只服务于"服务端自己拼的指标 SQL"(见 app/analytics/reports.py):
    走绑定参数而不是字符串内插, 既免转义又防注入。LLM 生成的 Text2SQL 一律
    不传 params —— 它已经被 sql_guard 按整条语句校验过, 加参数通道只会多一个
    绕过白名单的面。

    ``db_role``/``session_settings`` 是层 0/1 的两个接入点(analytics 域必传);
    其余三域不传时行为与改造前一致(属主连接 + 不注作用域变量)。
    """
    from app.db.sql_guard import validate_readonly_select

    safe_sql = validate_readonly_select(sql, allowed_tables)
    with scoped_connection(db_role, session_settings) as conn:
        result = conn.execute(text(safe_sql), params or {})
        cols = list(result.keys())
        rows = []
        for row in result.mappings():
            rows.append({k: _jsonify(v) for k, v in row.items()})
    # 只读: 不回滚也不取数据(提交同一事务同样立即释放快照与超时设置)。
    return [{"columns": cols, "rows": rows, "rowcount": len(rows)}]


class ScanCostExceeded(ValueError):
    """预估扫描行数超阈值(层 3 的成本预估拦截)。"""


def execute_scoped_select(
    sql: str,
    params: dict | None = None,
    *,
    db_role: str | None = None,
    session_settings: dict[str, str] | None = None,
    max_scan_rows: int | None = None,
) -> tuple[list[dict], int | None]:
    """执行一条**已由上层校验过**的 SELECT, 返回 ``(结果体, 预估扫描行数)``。

    与 :func:`execute_readonly_sql` 的差别只有一个: 不再跑正则 sql_guard —— analytics
    域已升级到 AST 校验(见 app/db/ast_guard.py), 那边给出的就是最终文本。执行与成本
    预估必须在**同一个作用域事务**里做: 预估要看到与执行同一份 RLS 过滤后的行数,
    否则"预估超限"与"实际能读到多少"是两个口径。
    """
    from app.db.ast_guard import estimate_scan_rows

    with scoped_connection(db_role, session_settings) as conn:
        scan_rows = estimate_scan_rows(conn, sql, params)
        if max_scan_rows is not None and scan_rows is not None and scan_rows > max_scan_rows:
            raise ScanCostExceeded(
                f"预估扫描 {scan_rows} 行, 超过上限 {max_scan_rows}, 已拒; 请缩小时间/部门范围"
            )
        result = conn.execute(text(sql), params or {})
        cols = list(result.keys())
        rows = [{k: _jsonify(v) for k, v in row.items()} for row in result.mappings()]
    return [{"columns": cols, "rows": rows, "rowcount": len(rows)}], scan_rows
