"""层 3: 基于 AST 的确定性 SQL 校验(只服务 analytics 域)。

为什么必须弃用正则黑名单: 正则防线在 ``/**/`` 穿插、``/*! ... */`` 可执行注释、
``0x616263`` 十六进制字面量、``CHR(97)||CHR(100)`` 拼接、大小写与全角变形面前**基本无效**
(见 docs-interview 里对 sql_guard 的复盘)。本模块的做法是: 解析成 AST -> 按结构判定 ->
**由 AST 重新生成 SQL**, 于是送进数据库的文本与模型给的文本已经不是同一份字符串,
"变形绕过"这一整类攻击面被结构性地消掉。

校验项(顺序即拒因顺序):
1. 语句数: 恰好 1 条, 多语句/堆查询直接拒;
2. 语句类型: 只读通道只许 SELECT/WITH...SELECT; 写通道只许 UPDATE/DELETE/INSERT,
   DDL(DROP/TRUNCATE/ALTER/CREATE)与命令(COPY/VACUUM/CALL/DO/GRANT...)一律拒;
3. 表白名单: 遍历所有表引用(含 JOIN、子查询、CTE 引用), 不在授权名单即拒;
   schema 限定名只允许 public(挡住 information_schema / pg_catalog);
4. 域谓词强制(写通道): WHERE 里必须存在 ``tenant_id = :scope_tenant`` 与
   ``dept_id = ANY(string_to_array(:scope_depts, ','))``, 且右侧必须是那个绑参占位符 ——
   字面量伪造一律拒;
5. 空 WHERE / 恒真 WHERE(``1=1``、``OR TRUE``)拒;
6. 函数与对象黑名单: pg_sleep / dblink / lo_import / lo_export / pg_read_file /
   set_config / current_setting / COPY ... PROGRAM 等;
7. 注释与编码: 结构上带注释即拒; 十六进制字面量与 ``CHR()``/拼接构造的值拒;
8. 子查询写操作: WHERE 中指向非白名单表的子查询在第 3 步就已被拒(遍历不分层级);
9. 行数上限: 只读通道强制补/收紧 LIMIT;
10. 成本预估: :func:`estimate_scan_rows` 用 EXPLAIN 取预估扫描行数, 交由调用方决定拒绝或降级。

边界: 本模块**不是**权限防线。真正兜底的是层 1 的 RLS 与最小权限角色 —— 这里放过一条
语句, 数据库仍然只会返回作用域内的行; 反过来这里拒一条语句, 也只是"不让它跑"。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlglot import exp, parse
from sqlglot.errors import ParseError

from app.config import get_settings
from app.db.scope import PARAM_DEPTS, PARAM_TENANT

logger = logging.getLogger(__name__)

READ_MODE = "read"
WRITE_MODE = "write"

# 语句类型: 读通道允许的 AST 根节点。
_READ_ROOTS = (exp.Select,)
# 写通道允许的根节点(INSERT/UPDATE/DELETE; SELECT 不在写通道里出现, dry-run 由服务端自己发)。
_WRITE_ROOTS = (exp.Insert, exp.Update, exp.Delete)

# 一眼就能认出来的"这不是查询也不是受控写"的节点: DDL / 命令 / 事务 / 会话操作。
# 这里的类名必须是 sqlglot 真有的(写错一个是导入时直接 AttributeError)。
_REJECTED_ROOTS = (
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,   # COPY / VACUUM / SET / CALL / DO / GRANT ... 都会落到这里
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Cache,
    exp.Lock,
    exp.Grant,
    exp.Merge,
    exp.Copy,
    exp.Set,
    exp.Show,
    exp.Use,
)

# 函数黑名单(PG 侧可逃逸到文件系统/网络/长阻塞的那一批)。
_FUNCTION_BLACKLIST = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_advisory_lock",
    "pg_cancel_backend", "pg_terminate_backend", "pg_reload_conf",
    "dblink", "dblink_exec", "dblink_connect",
    "lo_import", "lo_export", "lo_from_bytea",
    "pg_read_file", "pg_read_binary_file", "pg_write_file", "pg_ls_dir", "pg_stat_file",
    "set_config", "current_setting",   # 探针: 读/写会话作用域变量都不许模型碰
    "system", "pg_execute_server_program", "pg_ls_arch_dir",
    "load_file", "outfile", "dumpfile", "benchmark", "sleep", "xp_cmdshell",
}

# 只允许 unqualified 或 public.schema 的表引用。
_ALLOWED_SCHEMAS = {"", "public"}


class ASTGuardError(ValueError):
    """SQL 未通过 AST 校验。"""


def _int_limit(node: exp.Expression) -> int | None:
    """取 LIMIT 子句里的整数值(非字面量返回 None, 由调用方按\"收紧\"处理)。"""
    if node is None:
        return None
    expr = node.expression if isinstance(node, exp.Limit) else node
    if isinstance(expr, exp.Literal) and expr.is_int:
        try:
            return int(str(expr.this))
        except (TypeError, ValueError):
            return None
    return None


def _iter_function_names(root: exp.Expression) -> set[str]:
    """收集语句里出现过的函数名(含 Anonymous 形式的未知函数)。"""
    names: set[str] = set()
    for node in root.walk():
        if isinstance(node, exp.Anonymous):
            names.add(str(node.this or "").lower())
        elif isinstance(node, exp.Func):
            try:
                names.add((node.sql_name() or node.key or "").lower())
            except Exception:  # noqa: BLE001 - 个别函数类没有 sql_name
                names.add(node.key.lower())
    return names


def _has_comments(root: exp.Expression) -> bool:
    """结构上是否带注释(不看原文, 所以字符串字面量里的 ``--`` 不会误判)。"""
    return any(getattr(node, "comments", None) for node in root.walk())


def _is_tautology(node: exp.Expression | None) -> bool:
    """恒真判定: ``1=1``/``a=a``/``OR TRUE``/``TRUE`` 自身。"""
    if node is None:
        return False
    if isinstance(node, exp.Boolean):
        return node.this is True
    if isinstance(node, exp.Paren):
        return _is_tautology(node.this)
    if isinstance(node, exp.Or):
        return _is_tautology(node.left) or _is_tautology(node.right)
    if isinstance(node, exp.Xor):
        return _is_tautology(node.left) or _is_tautology(node.right)
    if isinstance(node, exp.EQ):
        left, right = node.left, node.right
        if left.sql(dialect="postgres") == right.sql(dialect="postgres"):
            return True
        return isinstance(right, exp.Boolean) and right.this is True
    if isinstance(node, exp.Is):
        return node.this.sql(dialect="postgres") == node.expression.sql(dialect="postgres")
    return False


def _has_tautological_term(node: exp.Expression | None) -> bool:
    """WHERE 里是否存在恒真的组成部分。

    两种都要拒: ``x OR TRUE`` 让整条 WHERE 变真; ``1=1 AND x`` 虽然等价于 ``x``,
    但它是"拼接时留了个永真条件"的典型形状(也是堆注入口条件时最常见的写法)。
    受控写里任何一层 AND 的叶子都该有真实含义, 永真叶子没有意义。
    """
    if node is None:
        return False
    if _is_tautology(node):
        return True
    if isinstance(node, (exp.And, exp.Or, exp.Xor)):
        return _has_tautological_term(node.left) or _has_tautological_term(node.right)
    if isinstance(node, exp.Paren):
        return _has_tautological_term(node.this)
    return False


def _require_scope_predicate(where: exp.Where | None) -> None:
    """写通道域谓词强制: 必须存在且**只许**是绑参形式的 tenant_id / dept_id 条件。

    只检查"形状"而不检查"值": 值由 :class:`app.db.scope.DataScope` 在服务端绑定, 模型
    拿不到绑参通道, 所以形状对了就不可能是它自己填的部门。
    """
    if where is None:
        raise ASTGuardError("写操作必须带 WHERE(全表变更一律拒)")
    condition = where.this
    if _has_tautological_term(condition):
        raise ASTGuardError("写操作的 WHERE 含恒真条件(1=1 / OR TRUE), 已拒")

    def _placeholder_names(node: exp.Expression) -> set[str]:
        return {
            str(p.name or "") for p in node.find_all(exp.Placeholder)
        }

    def _eq_for(name: str) -> exp.EQ | None:
        for eq in condition.find_all(exp.EQ):
            left = eq.left
            if isinstance(left, exp.Column) and left.name.lower() == name:
                return eq
        return None

    tenant_eq = _eq_for("tenant_id")
    if (
        tenant_eq is None
        or _placeholder_names(tenant_eq.right) != {PARAM_TENANT}
    ):
        raise ASTGuardError(
            "缺少租户谓词 tenant_id = :scope_tenant(该谓词由服务端注入, 模型不许自己给)"
        )
    dept_eq = _eq_for("dept_id")
    dept_sql = dept_eq.right.sql(dialect="postgres").lower() if dept_eq is not None else ""
    if (
        dept_eq is None
        or _placeholder_names(dept_eq.right) != {PARAM_DEPTS}
        or "string_to_array" not in dept_sql
        or "any(" not in dept_sql
    ):
        # 只认 "ANY(string_to_array(:scope_depts, ','))" 这一种形状: 字面量部门号、
        # IN 列表、子查询都走到这里被拒。
        raise ASTGuardError(
            "缺少部门谓词 dept_id = ANY(string_to_array(:scope_depts, ','))"
            "(该谓词由服务端注入, 模型不许自己给)"
        )


def _collect_tables(root: exp.Expression, allowed_tables: set[str]) -> set[str]:
    """遍历所有表引用并与白名单对账(CTE 别名除外)。"""
    cte_names = {
        (cte.alias_or_name or "").lower() for cte in root.find_all(exp.CTE)
    }
    tables: set[str] = set()
    for node in root.find_all(exp.Table):
        name = (node.name or "").lower()
        schema = (node.args.get("db").this if node.args.get("db") else "") or ""
        schema = str(schema).lower()
        if not name or name in cte_names:
            continue
        if schema not in _ALLOWED_SCHEMAS:
            raise ASTGuardError(f"不允许访问 schema {schema!r} 下的表 {name}")
        if name not in allowed_tables:
            raise ASTGuardError(
                f"仅允许查询业务白名单表 {sorted(allowed_tables)}, 非法表: {name}"
            )
        tables.add(name)
    return tables


def validate_sql(
    sql: str,
    *,
    allowed_tables: set[str],
    mode: str = READ_MODE,
    max_rows: int | None = None,
    normalize: bool = True,
) -> str:
    """校验一条 SQL; 默认返回**重新生成**的可执行文本(已按通道施加行数上限)。

    拒因都是确定性的: 同一份输入永远同一个结论, 不依赖模型判断也不依赖概率。

    ``normalize=False`` 只用于写模板自校验: sqlglot 的 postgres 方言会把 ``:name``
    占位符重写成 ``%(name)s``, 而 SQLAlchemy 的 ``text()`` 只认 ``:name`` —— 那种场合
    执行服务端自己写的模板文本, 不要让重写过的文本进绑参通道。
    """
    raw = (sql or "").strip().rstrip(";").strip()
    if not raw:
        raise ASTGuardError("SQL 为空")
    settings = get_settings()
    limit = int(max_rows if max_rows is not None else settings.sqlguard_max_rows)
    if len(raw) > settings.sqlguard_max_sql_length:
        raise ASTGuardError(f"SQL 超长 (>{settings.sqlguard_max_sql_length} 字符)")

    try:
        statements = [s for s in parse(raw, read="postgres") if s is not None]
    except ParseError as exc:
        raise ASTGuardError(f"SQL 解析失败(语法可疑): {exc}") from exc

    # 1. 语句数: 多语句/堆查询(尾部分号之外的第二条)直接拒。
    if len(statements) != 1:
        raise ASTGuardError(f"仅允许单条语句, 解析出 {len(statements)} 条")
    root = statements[0]

    # 7. 注释: 结构上有注释即拒(合法的 Text2SQL 不需要注释)。
    if _has_comments(root):
        raise ASTGuardError("不允许包含注释(注释是绕过式攻击的常见载体)")

    # 2. 语句类型白名单。
    if isinstance(root, _REJECTED_ROOTS):
        raise ASTGuardError(f"不允许的语句类型: {type(root).__name__}")
    if mode == READ_MODE:
        if not isinstance(root, _READ_ROOTS):
            raise ASTGuardError(f"只读通道仅允许 SELECT/WITH...SELECT, 收到 {type(root).__name__}")
    elif mode == WRITE_MODE:
        if not isinstance(root, _WRITE_ROOTS):
            raise ASTGuardError(f"写通道仅允许 INSERT/UPDATE/DELETE, 收到 {type(root).__name__}")
    else:  # 未知通道: 默认拒(不是默认放行)
        raise ASTGuardError(f"未知校验通道: {mode}")

    # 3. 表白名单(含 JOIN/子查询/CTE 引用)。
    _collect_tables(root, allowed_tables)

    # 6. 函数黑名单 + 7. 十六进制/CHR 拼接字面量。
    functions = _iter_function_names(root)
    bad = sorted(functions & _FUNCTION_BLACKLIST)
    if bad:
        raise ASTGuardError(f"禁止出现服务端函数: {', '.join(bad)}")
    if any(True for _ in root.find_all(exp.HexString)):
        raise ASTGuardError("禁止十六进制字面量")
    if any(True for _ in root.find_all(exp.Chr)):
        raise ASTGuardError("禁止 CHR() 拼接构造值")

    # 5. 恒真 WHERE(读通道也拒: 它是"拼条件没拼完"或注入形状的典型痕迹)
    where = root.args.get("where")
    if _has_tautological_term(where.this if where else None):
        raise ASTGuardError("WHERE 含恒真条件(如 1=1 / OR TRUE), 已拒")
    if mode == WRITE_MODE:
        # 4. 域谓词强制(写通道独有)。
        _require_scope_predicate(where)
        # 写通道的影响行数上限由层 4 的 dry-run 梯度负责, 这里不加 LIMIT:
        # PG 的 UPDATE/DELETE 没有 LIMIT, 想限住只能靠 CTID/子查询, 那是另一类风险。
    else:
        # 9. 行数上限: 缺失就补, 超阈值就收紧。
        existing = root.args.get("limit")
        n = _int_limit(existing) if existing is not None else None
        if n is None or n > limit:
            root = root.limit(limit)

    if not normalize:
        # 写模板自校验: 只回答"形状能不能过", 执行什么文本由调用方决定。
        # 解析已在上面完成, 所以"解析失败"这一类拒因不会因本开关而丢。
        return raw

    regenerated = root.sql(dialect="postgres", comments=False)
    if ";" in regenerated.rstrip(";"):
        # 重新生成后仍含分号(例如字符串字面量里的), 只对"结构上有第二条语句"敏感:
        # 用未加引号的分号判断, 交给解析器复核一次而不是正则。
        try:
            again = [s for s in parse(regenerated, read="postgres") if s is not None]
        except ParseError as exc:
            raise ASTGuardError(f"规范化后无法再解析: {exc}") from exc
        if len(again) != 1:
            raise ASTGuardError("规范化后不是单条语句, 已拒")
    return regenerated


def estimate_scan_rows(conn: Any, sql: str, params: dict | None = None) -> int | None:
    """用 EXPLAIN 取预估扫描行数(所有计划节点的 Plan Rows 之和)。

    失败返回 None 并由调用方决定"放行还是拒" —— 预估不出来本身不是攻击, 但也不能
    当成"预估通过"。
    """
    if not get_settings().sqlguard_explain_enabled:
        return None
    try:
        row = conn.execute(text(f"EXPLAIN (FORMAT JSON) {sql}"), params or {}).fetchone()
        raw = row[0] if row else None
    except Exception as exc:  # noqa: BLE001 - 预估失败不阻断, 由调用方判
        logger.warning("EXPLAIN 成本预估失败(跳过阈值判定): %s", exc)
        return None
    if raw is None:
        return None
    # psycopg3 会把 json 列直接解成 Python 对象, asyncpg/旧驱动则回字符串:
    # 只接一种的话, 另一条路上预估会永远变成 None 而静默失去成本防线。
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("EXPLAIN 输出不是合法 JSON(跳过阈值判定): %s", exc)
            return None
    else:
        payload = raw
    plan = payload[0].get("Plan") if isinstance(payload, list) else payload.get("Plan")
    if not plan:
        return None

    def _sum(node: dict) -> int:
        total = int(node.get("Plan Rows") or 0)
        for child in node.get("Plans") or []:
            total += _sum(child)
        return total

    return _sum(plan)
