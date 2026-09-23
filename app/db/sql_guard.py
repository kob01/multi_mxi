"""Read-only SQL guard for Text2SQL ``execute_sql`` tools.

Text2SQL 场景下由 LLM 生成 SQL, 这里做硬校验:
- 仅允许单条 SELECT / WITH ... SELECT 语句
- 关键字黑名单 (DML/DDL/服务端函数/元数据库)
- 表白名单 (每个 MCP server 传入自己的业务表集合)
- 强制行数上限 (MAX_ROWS), 结果集过大时改写 LIMIT
"""

from __future__ import annotations

import re

MAX_ROWS = 50
MAX_SQL_LENGTH = 2000

_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|rename|grant|revoke|"
    r"replace|merge|call|exec|execute|prepare|handler|lock|unlock|set|use|"
    r"load_file|outfile|dumpfile|sleep|benchmark|information_schema|"
    r"performance_schema|mysql|sys)\b",
    re.IGNORECASE,
)
# 结尾 LIMIT 子句 (用于改写超限行数)
_TAIL_LIMIT_RE = re.compile(r"\blimit\s+(\d+)(\s+offset\s+\d+)?\s*$", re.IGNORECASE)
# FROM / JOIN 后面的表名
_TABLE_RE = re.compile(r"\b(?:from|join)\s+`?([A-Za-z_]\w*)`?", re.IGNORECASE)
# CTE 别名: WITH [RECURSIVE] name AS (
_CTE_RE = re.compile(r"\bwith\s+(?:recursive\s+)?`?([A-Za-z_]\w*)`?\s+as\s*\(", re.IGNORECASE)


class SQLGuardError(ValueError):
    """SQL 未通过只读校验。"""


def validate_readonly_select(sql: str, allowed_tables: set[str]) -> str:
    """校验并规范化一条只读查询, 返回可直接执行的 SQL (带行数上限)。"""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SQLGuardError("SQL 为空")
    if len(sql) > MAX_SQL_LENGTH:
        raise SQLGuardError(f"SQL 超长 (>{MAX_SQL_LENGTH} 字符)")
    if ";" in sql:
        raise SQLGuardError("仅允许单条语句, 不能包含分号")
    if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
        raise SQLGuardError("仅允许 SELECT 查询语句")
    forbidden = _FORBIDDEN_RE.search(sql)
    if forbidden:
        raise SQLGuardError(f"禁止出现写操作或危险关键字: {forbidden.group(1)}")

    cte_names = set(_CTE_RE.findall(sql))
    tables = set(_TABLE_RE.findall(sql))
    unknown = tables - allowed_tables - cte_names
    if unknown:
        raise SQLGuardError(
            f"仅允许查询业务白名单表 {sorted(allowed_tables)}, 非法表: {sorted(unknown)}"
        )

    tail = _TAIL_LIMIT_RE.search(sql)
    if tail and int(tail.group(1)) > MAX_ROWS:
        sql = _TAIL_LIMIT_RE.sub(f"LIMIT {MAX_ROWS}", sql)
    elif not tail:
        sql = f"{sql} LIMIT {MAX_ROWS}"
    return sql


def inject_timeout_hint(sql: str, ms: int = 5000) -> str:
    """注入 MAX_EXECUTION_TIME 优化器提示 (MySQL 5.7.8+), 防慢查询拖垮连接。"""
    return re.sub(
        r"^select\b",
        f"SELECT /*+ MAX_EXECUTION_TIME({ms}) */",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
