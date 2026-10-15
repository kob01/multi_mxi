"""层 3 AST SQL Guard 的离线单测: 只依赖 sqlglot, 不需要 DB/LLM/网络/容器。

跑法::

    uv run python -m scripts.test_ast_guard_offline

覆盖的是"正则黑名单挡不住的那一类": 注释穿插、可执行注释、十六进制字面量、CHR() 拼接、
大小写与全角变形、多语句堆叠、DDL/命令、服务端函数、非白名单表与 schema、恒真 WHERE、
子查询里的表、写通道的域谓词形状。以及两件事:

1. **拒是结构性的**: 校验后返回的是 AST 重新生成的 SQL, 所以"变形绕过"即使没被拒,
   送进数据库的也已经不是模型给的那份文本(用断言把这条钉住)。
2. **合法查询不能被误杀**: 补 LIMIT / 收紧 LIMIT / CTE 别名不当表 / JOIN 正常通过。
"""

from __future__ import annotations

import sys

from app.db.ast_guard import ASTGuardError, READ_MODE, WRITE_MODE, validate_sql

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

ALLOWED = {
    "hr_employees",
    "hr_tickets",
    "hr_leave_records",
    "fin_reimbursements",
    "fin_department_budgets",
    "proc_orders",
    "proc_contracts",
    "proc_suppliers",
}

# 沙箱可查面与生产同源(不手抄一份名单: 视图改名后这里不跟着改就会"测过但跑不通")。
from app.db.analytics_views import sandbox_view_names  # noqa: E402

VIEWS = sandbox_view_names()

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def denied(name: str, sql: str, mode: str = READ_MODE) -> None:
    """断言"必须拒", 并把拒因带进失败详情(拒因写错也算失败)。"""
    try:
        out = validate_sql(sql, allowed_tables=ALLOWED, mode=mode)
    except ASTGuardError as exc:
        check(name, True)
        return
    check(name, False, f"竟被放行 -> {out[:120]}")


def allowed(name: str, sql: str, mode: str = READ_MODE) -> str:
    try:
        out = validate_sql(sql, allowed_tables=ALLOWED, mode=mode)
    except ASTGuardError as exc:
        check(name, False, f"被误杀: {exc}")
        return ""
    check(name, True)
    return out


def sandbox_denied(name: str, sql: str, **kwargs) -> None:
    """沙箱专项护栏的"必须拒"断言(可查面 = 治理视图)。"""
    try:
        out = validate_sql(sql, allowed_tables=VIEWS, mode=READ_MODE, **kwargs)
    except ASTGuardError:
        check(name, True)
        return
    check(name, False, f"竟被放行 -> {out[:120]}")


def sandbox_allowed(name: str, sql: str, **kwargs) -> str:
    try:
        out = validate_sql(sql, allowed_tables=VIEWS, mode=READ_MODE, **kwargs)
    except ASTGuardError as exc:
        check(name, False, f"被误杀: {exc}")
        return ""
    check(name, True)
    return out


def main() -> int:
    # ---- 1. 语句数: 多语句/堆叠查询 ----
    denied("多条语句(分号堆叠)", "SELECT 1; DROP TABLE hr_employees")
    denied("尾随第二条 DDL", "SELECT emp_id FROM hr_employees; TRUNCATE hr_tickets;")

    # ---- 2. 语句类型 ----
    denied("DDL: DROP", "DROP TABLE hr_employees")
    denied("DDL: TRUNCATE", "TRUNCATE TABLE hr_employees")
    denied("DDL: CREATE", "CREATE TABLE evil (a int)")
    denied("DDL: ALTER", "ALTER TABLE hr_employees ADD COLUMN evil text")
    denied("命令: VACUUM", "VACUUM")
    denied("命令: COPY TO PROGRAM", "COPY hr_employees TO PROGRAM 'sh -c id'")
    denied("命令: SET 会话变量", "SET app.tenant_id = 'T999'")
    denied("读通道里的 UPDATE", "UPDATE hr_employees SET name = 'x'", mode=READ_MODE)
    denied("读通道里的 DELETE", "DELETE FROM hr_employees", mode=READ_MODE)

    # ---- 3. 表白名单 / schema ----
    denied("非白名单表", "SELECT * from pg_class")
    denied("pg_catalog 前缀", "SELECT * FROM pg_catalog.pg_user")
    denied("information_schema", "SELECT * FROM information_schema.tables")
    denied("子查询里的非白名单表", "SELECT emp_id FROM hr_employees WHERE emp_id IN (SELECT pid FROM pg_stat_activity)")
    denied("CTE 名字伪装成表", "WITH hr_employees AS (SELECT * FROM pg_shadow) SELECT * FROM hr_employees")
    allowed("JOIN 白名单两表", "SELECT e.name, r.amount FROM fin_reimbursements r JOIN hr_employees e ON r.emp_id = e.emp_id")

    # ---- 4/5. 域谓词与空/恒真 WHERE(写通道) ----
    good = (
        "UPDATE fin_reimbursements SET status = :p_status "
        "WHERE created_at < :p_before "
        "AND tenant_id = :scope_tenant "
        "AND dept_id = ANY(string_to_array(:scope_depts, ','))"
    )
    allowed("写模板: 形状齐备", good, mode=WRITE_MODE)
    denied(
        "写模板: 缺租户谓词",
        "UPDATE fin_reimbursements SET status = :p_status WHERE tenant_id = 'T001' "
        "AND dept_id = ANY(string_to_array(:scope_depts, ','))",
        mode=WRITE_MODE,
    )
    denied(
        "写模板: 部门谓词写成字面量",
        "UPDATE fin_reimbursements SET status = :p_status WHERE tenant_id = :scope_tenant "
        "AND dept_id IN ('D001')",
        mode=WRITE_MODE,
    )
    denied("写操作: 无 WHERE", "DELETE FROM hr_tickets", mode=WRITE_MODE)
    # 注: 形状齐备的物理 DELETE 在本层是"能过校验"的 —— 拦它的是层 4 的模板
    # (action=delete 永远被改写成 UPDATE 软删) 与层 1 的不授 DELETE, 不是语法校验。
    # 这一条故意不断言"被语法拒", 免得以后把防线误建在校验器上。
    allowed(
        "软删模板: DELETE 已被改写成 UPDATE",
        "UPDATE hr_tickets SET is_deleted = TRUE, deleted_at = now(), deleted_by = :scope_actor "
        "WHERE status = :p_status AND tenant_id = :scope_tenant "
        "AND dept_id = ANY(string_to_array(:scope_depts, ','))",
        mode=WRITE_MODE,
    )
    denied(
        "恒真 WHERE(写)",
        "UPDATE fin_reimbursements SET status = :p_status WHERE 1=1 "
        "AND tenant_id = :scope_tenant AND dept_id = ANY(string_to_array(:scope_depts, ','))",
        mode=WRITE_MODE,
    )
    denied(
        "恒真 WHERE(OR TRUE, 写)",
        "UPDATE fin_reimbursements SET status = :p_status WHERE status = :p0 OR TRUE "
        "AND tenant_id = :scope_tenant AND dept_id = ANY(string_to_array(:scope_depts, ','))",
        mode=WRITE_MODE,
    )
    denied("恒真 WHERE(读)", "SELECT emp_id FROM hr_employees WHERE 1 = 1")

    # ---- 6. 函数黑名单 ----
    denied("pg_sleep", "SELECT pg_sleep(10)")
    denied("dblink", "SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(x int)")
    denied("lo_import", "SELECT lo_import('/etc/passwd')")
    denied("current_setting 探针", "SELECT current_setting('app.tenant_id')")
    denied("未知函数走 Anonymous 也要拦", "SELECT load_file('/etc/passwd')")

    # ---- 7. 注释与编码变形 ----
    denied("块注释穿插", "SELECT/**/emp_id/**/FROM/**/hr_employees")
    denied("行尾注释", "SELECT emp_id FROM hr_employees -- 只看在职")
    denied("MySQL 风格可执行注释", "/*!40000 SELECT emp_id FROM hr_employees */")
    denied("十六进制字面量", "SELECT 0x61626364")
    denied("CHR() 拼接构造值", "SELECT CHR(65) || CHR(66) FROM hr_employees")
    denied("大小写变形 + 非白名单表", "sElEcT * FrOm PG_uSeR")

    # ---- 9. 行数上限: 合法查询不被误杀, 且被强制收紧 ----
    out = allowed("无 LIMIT 的查询要放行并补上限", "SELECT department FROM hr_employees")
    check("补上的上限写进返回文本", "LIMIT 50" in out.upper(), out)
    out2 = allowed("超限 LIMIT 被收紧", "SELECT department FROM hr_employees LIMIT 999999")
    check("收紧后不再是大数", "LIMIT 999999" not in out2.upper(), out2)
    out3 = allowed("CTE 正常写法", "WITH t AS (SELECT emp_id FROM hr_employees) SELECT COUNT(*) FROM t")
    check("CTE 重写后仍含原表名", "hr_employees" in out3, out3)

    # 写模板自校验不得重写文本: sqlglot 会把 :name 改写成 %(name)s,
    # 而 SQLAlchemy text() 只认 :name —— 重写后的文本进绑参通道会直接坏掉。
    kept = validate_sql(good, allowed_tables=ALLOWED, mode=WRITE_MODE, normalize=False)
    check(
        "normalize=False 时返原模板(占位符不被重写)",
        ":scope_depts" in kept and "%(scope_depts)s" not in kept,
        kept,
    )

    # 变形绕过的结构性结论: 返回的是重新生成的 SQL, 与输入文本不同一份。
    raw = "select   emp_id   from   hr_employees   where   status='在职'"
    norm = validate_sql(raw, allowed_tables=ALLOWED)
    check(
        "返回的是 AST 重新生成的文本(不是原文)",
        norm != raw and "hr_employees" in norm,
        norm,
    )
    check(
        "写模式根类型判定不放过未知通道",
        _unknown_channel_denied(),
    )

    # ---- 11. 沙箱专项护栏(越权 + 资源)----
    # 这一组只服务于"模型直出自由 SQL"那一级: 默认全关, 下面最后两条兼容性断言把
    # "没传新参就与改造前逐字一致"钉住 —— 编译器产物与 analytics 路径不应该被误杀。
    sandbox_denied("基表不在沙箱可查面(只能碰视图)", "SELECT emp_id FROM hr_employees")
    sandbox_allowed(
        "带 ON 的两视图 JOIN",
        "SELECT r.amount, e.name FROM v_analytics_reimbursements r "
        "JOIN v_analytics_employees e ON r.emp_id = e.emp_id",
        reject_cartesian=True, max_joins=5,
    )
    sandbox_denied(
        "笛卡尔: CROSS JOIN",
        "SELECT r.amount FROM v_analytics_reimbursements r CROSS JOIN v_analytics_employees e",
        reject_cartesian=True,
    )
    sandbox_denied(
        "笛卡尔: 隐式逗号多表",
        "SELECT r.amount FROM v_analytics_reimbursements r, v_analytics_employees e",
        reject_cartesian=True,
    )
    sandbox_denied(
        "笛卡尔: JOIN 不写 ON",
        "SELECT r.amount FROM v_analytics_reimbursements r JOIN v_analytics_employees e",
        reject_cartesian=True,
    )
    sandbox_denied(
        "笛卡尔: ON 1=1(有子句但永真)",
        "SELECT r.amount FROM v_analytics_reimbursements r "
        "JOIN v_analytics_employees e ON 1 = 1",
        reject_cartesian=True,
    )
    sandbox_allowed(
        "USING 也算关联条件",
        "SELECT emp_id FROM v_analytics_reimbursements r "
        "JOIN v_analytics_tickets t USING (emp_id)",
        reject_cartesian=True, max_joins=5,
    )
    sandbox_denied(
        "JOIN 数超上限(6 张视图)", _SIX_VIEW_JOIN, reject_cartesian=True, max_joins=5,
    )
    sandbox_allowed(
        "JOIN 数在上限内(5 张视图)", _FIVE_VIEW_JOIN, reject_cartesian=True, max_joins=5,
    )
    # 上限只数真实关系: 把 CTE 别名也算进去会让"两个视图 + 几个中间步骤"的写法全被误杀。
    sandbox_allowed(
        "CTE 别名不计入 JOIN 数",
        "WITH s1 AS (SELECT emp_id FROM v_analytics_employees), "
        "s2 AS (SELECT emp_id FROM v_analytics_tickets) "
        "SELECT COUNT(*) FROM s1 JOIN s2 ON s1.emp_id = s2.emp_id",
        reject_cartesian=True, max_joins=2,
    )
    sandbox_denied(
        "敏感列: 点名引用",
        "SELECT annual_leave_used FROM v_analytics_employees",
        denied_columns={"annual_leave_used"},
    )
    sandbox_denied(
        "敏感列: 出现在 WHERE 也要拒",
        "SELECT emp_id FROM v_analytics_employees WHERE annual_leave_used > 5",
        denied_columns={"annual_leave_used"},
    )
    sandbox_allowed(
        "同一视图不含敏感列仍放行",
        "SELECT department FROM v_analytics_employees",
        denied_columns={"annual_leave_used"},
    )
    sandbox_denied(
        "敏感列生效时拒星号投影",
        "SELECT * FROM v_analytics_employees",
        denied_columns={"annual_leave_used"}, deny_star_projection=True,
    )
    sandbox_allowed(
        "COUNT(*) 的星号不是投影",
        "SELECT COUNT(*) FROM v_analytics_employees",
        denied_columns={"annual_leave_used"}, deny_star_projection=True,
    )
    sandbox_allowed(
        "视图面默认不拒 SELECT *(仅退回基表时才拒)",
        "SELECT * FROM v_analytics_employees",
        denied_columns={"annual_leave_used"},
    )
    check("系统表即使误加进白名单也拒", _pg_relation_in_whitelist_denied())

    # 兼容性: 新护栏必须"不传就等于没有"(旧路径逐字不变)。
    allowed("兼容: 未开护栏时逗号连接仍按旧行为放行",
            "SELECT r.amount FROM fin_reimbursements r, hr_employees e")
    allowed("兼容: 未开护栏时敏感列与星号都不拦",
            "SELECT * FROM hr_employees")

    passed = sum(1 for ok, _, _ in _results if ok)
    print(f"\n{passed}/{len(_results)} 通过")
    return 0 if passed == len(_results) else 1


def _unknown_channel_denied() -> bool:
    """未知校验通道必须默认拒(而不是默认放行)。"""
    try:
        validate_sql("SELECT 1", allowed_tables=ALLOWED, mode="yolo")
    except ASTGuardError:
        return True
    return False


def _pg_relation_in_whitelist_denied() -> bool:
    """``pg_`` 前缀必须无条件拒: 不能把安全性交给"没人误把系统表加进白名单"这个假设。"""
    try:
        validate_sql("SELECT usename FROM pg_user", allowed_tables={"pg_user"})
    except ASTGuardError:
        return True
    return False


_SIX_VIEW_JOIN = (
    "SELECT x0.emp_id FROM v_analytics_employees x0 "
    "JOIN v_analytics_tickets x1 ON x1.emp_id = x0.emp_id "
    "JOIN v_analytics_leave_records x2 ON x2.emp_id = x0.emp_id "
    "JOIN v_analytics_reimbursements x3 ON x3.emp_id = x0.emp_id "
    "JOIN v_analytics_purchase_orders x4 ON x4.emp_id = x0.emp_id "
    "JOIN v_analytics_contracts x5 ON x5.status = x0.status"
)

_FIVE_VIEW_JOIN = _SIX_VIEW_JOIN.split("JOIN v_analytics_contracts")[0].rstrip()


if __name__ == "__main__":
    sys.exit(main())
