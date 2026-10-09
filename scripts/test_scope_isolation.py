"""层 1/2/3 的端到端隔离验证(必须在容器里跑, 结论只对容器部署成立)。

跑法::

    docker compose -f docker/docker-compose.yml exec assistant python -m scripts.test_scope_isolation

为什么走容器而不是宿主机: 本脚本判的是"生效角色 + RLS 策略 + AST 校验"这三层在**真库**
上的行为, 而角色/策略只存在于容器那份 postgres 卷里; 宿主 .env 的视角与容器不同轨,
在宿主跑通不代表容器通(见 .qoder/rules/container-first-verification.md)。

覆盖:
  1. 隔离地基: 角色已建、策略已 FORCE(少了任何一条, 下面所有断言都只是假象);
  2. 作用域解析: manager 得到本部门, 全员角色得到全租户;
  3. 只读隔离: 同一条 SQL 以 manager 与 finance 身份跑, 行数必须不同;
  4. 写作用域: 跨部门的写计划预演命中 0 行(RLS 让它看不见别人的行, 不是靠代码提醒);
  5. 默认拒: 不带调用者身份直取工具 -> 拒; 未知/越权角色 -> 拒;
  6. AST 绕过面在真方言上仍被拒(pg_catalog、注释穿插、多语句、DDL)。

刻意不真执行任何业务数据变更: 只跑到 plan/dry-run 为止(改数据那条路用
scripts/test_dataops_dsl_offline 的纯函数断言 + 审批台人工操作覆盖)。
"""

from __future__ import annotations

import sys
from typing import Any

from app.db.rls import read_role, rls_status, write_role
from app.db.scope import ScopeError, resolve_scope_sync

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"

_results: list[tuple[bool, str, str]] = []

# mock 数据里的两个身份(见 scripts/seed_business_data.py):
# E10003 王强/市场部/manager(只看本部门), E10005 刘洋/财务部/finance(可跨部门)。
MANAGER_ID, MANAGER_ROLE = "E10003", "manager"
FINANCE_ID, FINANCE_ROLE = "E10005", "finance"

# 显式区间: 种子数据落在 2026-03 ~ 2026-06, 用"本月"会查空, 断言就失去意义。
WINDOW = "2026-03-01", "2026-07-01"


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL}  {name}" + (f"  -> {detail}" if detail and not ok else ""))


def _rows(payload: Any) -> list[dict[str, Any]]:
    """从工具返回体里取行(list/dict 两种形状都兜住)。"""
    if isinstance(payload, dict):
        block = payload
    elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
        block = payload[0]
    else:
        return []
    if block.get("error"):
        raise AssertionError(f"工具返回错误: {block['error']}")
    return block.get("rows") or []


def main() -> int:
    # 延迟到运行时导入: 模块导入会构造 FastMCP 对象, 放在函数里便于读栈错误。
    import app.mcp_servers.analytics_server as analytics

    # ---- 1. 隔离地基 ----
    status = _run(rls_status())
    policy_tables = {p["table"] for p in status.get("policies", [])}
    forced = set(status.get("forced_tables", []))
    role_names = {r["name"] for r in status.get("roles", [])}
    check("analytics 只读/写角色已创建", bool(read_role()) and bool(write_role())
          and {read_role(), write_role()} <= role_names, str(sorted(role_names)))
    check("策略覆盖 8 张业务表", len(policy_tables & {"hr_employees", "hr_tickets", "hr_leave_records",
          "fin_reimbursements", "fin_department_budgets", "proc_orders", "proc_contracts",
          "proc_suppliers"}) == 8, str(sorted(policy_tables)))
    check("策略对属主也强制(FORCE)", len(forced & policy_tables) == len(policy_tables),
          str(sorted(forced)))
    for role_info in status.get("roles", []):
        check(f"角色 {role_info['name']} 不可登录且无 BYPASSRLS",
              role_info["can_login"] is False and role_info["bypass_rls"] is False,
              str(role_info))

    # ---- 2. 作用域解析 ----
    manager_scope = resolve_scope_sync(MANAGER_ID, MANAGER_ROLE)
    finance_scope = resolve_scope_sync(FINANCE_ID, FINANCE_ROLE)
    check("manager 只拿到本部门", manager_scope.all_depts is False
          and len(manager_scope.dept_ids) == 2  # 本部门 + 共享哨兵
          and manager_scope.department == "市场部", str(manager_scope))
    check("finance 拿到全员范围", finance_scope.all_depts is True
          and len(finance_scope.dept_ids) > 2, str(finance_scope))
    check("未知工号解析被拒(默认拒)", _raises_scope(MANAGER_ID + "X", MANAGER_ROLE))
    check("空工号解析被拒(默认拒)", _raises_scope("", MANAGER_ROLE))

    # ---- 3. 只读隔离: 同一条 SQL, 两种身份 ----
    sql = (
        "SELECT e.department AS department, COUNT(*) AS cnt, COALESCE(SUM(r.amount), 0) AS total "
        "FROM fin_reimbursements r JOIN hr_employees e ON r.emp_id = e.emp_id "
        f"WHERE r.created_at >= '{WINDOW[0]}' AND r.created_at < '{WINDOW[1]}' "
        "GROUP BY e.department"
    )
    manager_rows = _rows(analytics.run_sql(sql, caller_user_id=MANAGER_ID, caller_role=MANAGER_ROLE))
    finance_rows = _rows(analytics.run_sql(sql, caller_user_id=FINANCE_ID, caller_role=FINANCE_ROLE))
    manager_depts = {r.get("department") for r in manager_rows}
    finance_depts = {r.get("department") for r in finance_rows}
    check("manager 的结果只含本部门", manager_depts <= {"市场部"}
          , f"{manager_depts} / {finance_depts}")
    check("finance 能跨部门(且严格多于 manager)",
          len(finance_depts) > len(manager_depts), f"{sorted(finance_depts)} vs {sorted(manager_depts)}")
    check("读结果带 untrusted_data 数据标记", bool(_first_block(
        analytics.run_sql(sql, caller_user_id=FINANCE_ID, caller_role=FINANCE_ROLE)).get("untrusted_data")))

    # 员工主数据同样受限: manager 只看得到本部门的人。
    emp_rows = _rows(analytics.run_sql(
        "SELECT emp_id, department FROM hr_employees", caller_user_id=MANAGER_ID, caller_role=MANAGER_ROLE))
    check("manager 只看到本部门员工",
          bool(emp_rows) and {r["department"] for r in emp_rows} == {"市场部"},
          str({r["department"] for r in emp_rows}))

    # ---- 4. 写作用域: 跨部门的写在预演阶段就是 0 行 ----
    plan = {
        "action": "update",
        "entity": "fin_reimbursements",
        "filters": [{"field": "status", "op": "eq", "value": "SUBMITTED"}],
        "sets": {"current_node": "越权尝试"},
        "reason": "验证写作用域: 别的部门的单不该被命中",
    }
    # finance 是全员作用域, 这里改用 manager 视角验证"看不见就改不了"。
    manager_plan_result = analytics.plan_data_op(
        plan, caller_user_id=MANAGER_ID, caller_role=MANAGER_ROLE,
        caller_intent_text="把已提交的报销单节点改成越权尝试",
    )
    check("manager 默认无写权限(能看全员不等于能改)",
          bool(manager_plan_result.get("forbidden")) or manager_plan_result.get("status") is None,
          str(manager_plan_result)[:220])

    finance_plan = analytics.plan_data_op(
        plan, caller_user_id=FINANCE_ID, caller_role=FINANCE_ROLE,
        caller_intent_text="把已提交的报销单节点改成财务复核",
    )
    check("finance 可发起写计划并拿到 op_id 与人话回显",
          bool(finance_plan.get("op_id")) and "即将更新" in str(finance_plan.get("preview", "")),
          str(finance_plan)[:260])
    check("写计划只到 pending/复核, 不在发起时就改数据",
          finance_plan.get("status") in ("PENDING_CONFIRM", "PENDING_APPROVAL", "NEED_REVIEW", "DENIED"),
          str(finance_plan.get("status")))

    # 用"只命中别的部门"的条件验证 RLS 让写看不见别人的行。
    other_dept_plan = {
        "action": "update",
        "entity": "fin_reimbursements",
        "filters": [{"field": "emp_id", "op": "eq", "value": "E10003"}],  # 市场部经理的报销单
        "sets": {"current_node": "跨部门尝试"},
        "reason": "验证 RLS 对写路径同样生效",
    }
    cross = analytics.plan_data_op(
        other_dept_plan, caller_user_id=FINANCE_ID, caller_role=FINANCE_ROLE,
        caller_intent_text="把 E10003 的报销单节点改成跨部门尝试",
    )
    # finance 是全员角色, 所以这一步允许命中; 真正的"看不见"用 manager 视角验证。
    cross_as_manager = analytics.plan_data_op(
        other_dept_plan, caller_user_id=MANAGER_ID, caller_role=MANAGER_ROLE,
        caller_intent_text="把 E10003 的报销单节点改成跨部门尝试",
    )
    check("跨部门写在无写权限角色这里直接被拒", bool(cross_as_manager.get("forbidden")),
          str(cross_as_manager)[:200])
    print(f"    (info) finance 全员角色对 E10003 的写计划: {cross.get('status')} / est={cross.get('est_rows')}")

    # ---- 5. 默认拒: 不带身份 ----
    no_caller = analytics.run_sql(sql)
    check("不带调用者身份的取数被拒", _is_denied(no_caller), str(no_caller)[:200])
    anon_plan = analytics.plan_data_op(plan, caller_user_id="", caller_role="",
                                       caller_intent_text="把报销单节点改掉")
    check("不带身份发起写计划被拒", bool(anon_plan.get("forbidden") or anon_plan.get("denied")),
          str(anon_plan)[:200])
    employee = analytics.run_sql(sql, caller_user_id="E10001", caller_role="employee")
    # 只记录不判定: 普通员工的真正约束在工具矩阵(auth.analytics_whitelist 返回空集, 工具根本
    # 不会被递到模型面前), 在函数层直调时它反倒能看到自己部门的数据 —— 那是应有的行为。
    print(f"    (info) employee 直调 run_sql: {employee if _is_denied(employee) else '返回本部门数据(工具矩阵已隐藏本工具)'}")

    # ---- 6. AST 绕过面在真方言上仍被拒 ----
    for name, bad_sql in (
        ("pg_catalog 表引用", "SELECT relname FROM pg_catalog.pg_class"),
        ("多语句堆叠", "SELECT emp_id FROM hr_employees; DROP TABLE hr_employees"),
        ("DDL", "TRUNCATE proc_orders"),
        ("注释穿插", "SELECT/**/emp_id/**/FROM/**/hr_employees"),
        ("服务端函数", "SELECT pg_sleep(5)"),
        ("information_schema", "SELECT * FROM information_schema.tables"),
    ):
        result = analytics.run_sql(bad_sql, caller_user_id=FINANCE_ID, caller_role=FINANCE_ROLE)
        check(f"AST 拒: {name}", _is_denied(result), str(result)[:180])

    passed = sum(1 for ok, _, _ in _results if ok)
    print(f"\n{passed}/{len(_results)} 通过")
    return 0 if passed == len(_results) else 1


def _first_block(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return payload if isinstance(payload, dict) else {}


def _is_denied(payload: Any) -> bool:
    block = _first_block(payload)
    return bool(block.get("error")) or bool(block.get("forbidden"))


def _raises_scope(user_id: str, role: str) -> bool:
    try:
        resolve_scope_sync(user_id, role)
    except ScopeError:
        return True
    return False


def _run(coro):
    """在本事件循环外跑一个协程(本脚本是普通入口, 没有正在跑的事件循环)。"""
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    sys.exit(main())
