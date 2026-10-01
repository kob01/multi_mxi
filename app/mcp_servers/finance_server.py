"""Finance reimbursement MCP Server (FastMCP, streamable-http transport).

Exposes the enterprise finance system stored in PostgreSQL
(fin_reimbursements / fin_department_budgets) as MCP tools; hr_employees is
readable for employee->department joins. Also exposes a read-only Text2SQL
tool (execute_sql) with a hard table whitelist.

归属校验与 hr_server 同一口径(见 app/security/caller.py): 身份由网关注入的
``caller_user_id``/``caller_role`` 提供, 员工只能提/看自己的报销单, 管理角色才能
跨人; 不带调用者的直连一律拒。

Run:
    python -m app.mcp_servers.finance_server     # serves http://0.0.0.0:8002/mcp
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import sync as dbsync
from app.db.models import DepartmentBudget, Reimbursement
from app.db.sequences import next_numbered
from app.db.sql_guard import SQLGuardError
from app.security.caller import guard_owner, guard_target_user, resolve_caller

mcp = FastMCP("finance-reimbursement-system", host="0.0.0.0", port=8002)

ALLOWED_CATEGORIES = {"差旅费", "交通费", "餐饮费", "办公用品", "培训费"}
SINGLE_LIMIT = 5000.0  # per-order limit (CNY)
# Text2SQL 表白名单: 财务两张业务表 + hr_employees (员工->部门映射, 供按部门统计)
ALLOWED_TABLES = {"fin_reimbursements", "fin_department_budgets", "hr_employees"}


def _next_order_no(session: Session) -> str:
    """取报销单号: 走序列而非 MAX+1(并发不重号)。"""
    return next_numbered(
        session,
        sequence="fin_order_no_seq",
        prefix="FIN",
        table="fin_reimbursements",
        column="order_no",
        start=5000,
    )


def _order_dict(o: Reimbursement) -> dict[str, Any]:
    return {
        "order_no": o.order_no,
        "user_id": o.emp_id,
        "title": o.title,
        "amount": float(o.amount),
        "category": o.category,
        "reason": o.reason,
        "status": o.status,
        "current_node": o.current_node,
        "created_at": o.created_at.isoformat(timespec="seconds"),
    }


@mcp.tool()
def create_reimbursement(
    title: str,
    amount: float,
    category: str,
    reason: str = "",
    user_id: str = "",
    caller_user_id: str = "",
    caller_role: str = "",
) -> dict[str, Any]:
    """Submit a reimbursement order (默认给本人提单; 代他人提单需管理角色)。

    Args:
        title: Expense title, e.g. 上海出差高铁票.
        amount: Amount in CNY; must be positive and <= 5000 per order.
        category: One of 差旅费/交通费/餐饮费/办公用品/培训费.
        reason: Business justification (optional).
        user_id: 报销人工号; 留空即当前调用者本人。
        caller_user_id: 调用者工号(网关注入, 请勿自行填写)。
        caller_role: 调用者角色(网关注入, 请勿自行填写)。

    Returns:
        Created order with order_no and workflow status, or error payload.
    """
    if category not in ALLOWED_CATEGORIES:
        return {"error": f"非法报销类别: {category}; 可选: {sorted(ALLOWED_CATEGORIES)}"}
    if amount <= 0:
        return {"error": "金额必须大于 0"}
    if amount > SINGLE_LIMIT:
        return {"error": f"单笔报销上限 {SINGLE_LIMIT} 元, 请拆分后提交"}
    effective, denial = guard_target_user(
        resolve_caller(caller_user_id, caller_role), user_id, what="报销单"
    )
    if denial is not None:
        return denial

    with Session(dbsync.get_sync_engine()) as session:
        try:
            order = Reimbursement(
                order_no=_next_order_no(session),
                emp_id=effective,
                title=title,
                amount=round(amount, 2),
                category=category,
                reason=reason,
                status="SUBMITTED",
                current_node="部门主管审批",
                # timestamptz 列: 必须传带时区的值
                created_at=datetime.now(timezone.utc),
            )
            session.add(order)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            return {"error": f"报销单写入冲突({exc.__class__.__name__}), 请重试一次"}
        except SQLAlchemyError as exc:
            session.rollback()
            return {"error": f"报销单创建失败({exc.__class__.__name__}), 请稍后重试"}
        return _order_dict(order)


@mcp.tool()
def query_reimbursement(
    order_no: str, caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """Query a reimbursement order by order number (只能看自己的, 管理角色除外)。

    Args:
        order_no: Order number, e.g. FIN5000.
        caller_user_id: 调用者工号(网关注入, 请勿自行填写)。
        caller_role: 调用者角色(网关注入, 请勿自行填写)。

    Returns:
        Order record or error payload.
    """
    caller = resolve_caller(caller_user_id, caller_role)
    with Session(dbsync.get_sync_engine()) as session:
        order = session.get(Reimbursement, order_no)
        if order is None:
            return {"error": f"order {order_no} not found"}
        denial = guard_owner(caller, order.emp_id, what="报销单")
        if denial is not None:
            return denial
        return _order_dict(order)


@mcp.tool()
def list_reimbursements(
    user_id: str = "", caller_user_id: str = "", caller_role: str = ""
) -> list[dict[str, Any]] | dict[str, Any]:
    """List reimbursement orders of an employee (留空 = 列本人)。

    Args:
        user_id: 目标员工工号; 留空即本人。
        caller_user_id: 调用者工号(网关注入, 请勿自行填写)。
        caller_role: 调用者角色(网关注入, 请勿自行填写)。

    Returns:
        List of orders (may be empty), or {error} on 越权。
    """
    effective, denial = guard_target_user(
        resolve_caller(caller_user_id, caller_role), user_id, what="报销单"
    )
    if denial is not None:
        return denial
    with Session(dbsync.get_sync_engine()) as session:
        orders = session.scalars(
            select(Reimbursement)
            .where(Reimbursement.emp_id == effective)
            .order_by(Reimbursement.created_at.desc())
        ).all()
        return [_order_dict(o) for o in orders]


@mcp.tool()
def finance_budget_query(department: str) -> dict[str, Any]:
    """Query a department's annual budget usage.

    注意: 本工具为敏感工具, 仅对部门经理/HR/财务专员等管理角色可见。

    Args:
        department: Department name, e.g. 研发部.

    Returns:
        Budget summary with annual / used / remaining, or error payload.
    """
    with Session(dbsync.get_sync_engine()) as session:
        budget = session.scalars(
            select(DepartmentBudget)
            .where(DepartmentBudget.department == department)
            .order_by(DepartmentBudget.year.desc())
            .limit(1)
        ).first()
        if budget is None:
            known = session.scalars(select(DepartmentBudget.department).distinct()).all()
            return {"error": f"未知部门: {department}; 可选: {sorted(known)}"}
        remaining = round(float(budget.annual_budget) - float(budget.used_amount), 2)
        return {
            "department": budget.department,
            "year": budget.year,
            "annual": float(budget.annual_budget),
            "used": float(budget.used_amount),
            "remaining": remaining,
        }


@mcp.tool()
def get_reimbursement_policy(category: str) -> dict[str, Any]:
    """Fetch the reimbursement policy snippet for a category.

    Args:
        category: Expense category.

    Returns:
        Policy description with limit and required attachments.
    """
    policies = {
        "差旅费": "差旅费按城市等级限额, 需附行程单/发票, 单笔≤5000元",
        "交通费": "市内交通实报实销, 需附发票, 单笔≤5000元",
        "餐饮费": "业务招待需事前审批, 需附发票与接待清单",
        "办公用品": "需附采购清单与发票, 单笔≤5000元",
        "培训费": "需培训通知与发票, 年度额度20000元",
    }
    if category not in policies:
        return {"error": f"未知类别: {category}; 可选: {sorted(policies)}"}
    return {"category": category, "policy": policies[category]}


@mcp.tool()
def execute_sql(sql: str) -> list[dict[str, Any]]:
    """Text2SQL: 在财务业务库上执行一条只读 SELECT 查询并返回结果。

    仅可查询以下表: fin_reimbursements(报销单), fin_department_budgets(部门年度预算),
    hr_employees(员工主数据, 可用于员工->部门关联统计)。仅允许单条 SELECT;
    禁止写操作; 结果最多返回 50 行。优先使用专用工具(create_reimbursement 等)
    完成业务操作, 本工具用于统计/明细等灵活查询。

    Args:
        sql: A single read-only PostgreSQL SELECT statement against whitelisted tables.

    Returns:
        One result object: {columns, rows, rowcount}, or an error payload list.
    """
    try:
        return dbsync.execute_readonly_sql(sql, ALLOWED_TABLES)
    except SQLGuardError as exc:
        return [{"error": f"SQL 校验失败: {exc}", "sql": sql}]
    except SQLAlchemyError as exc:
        return [{"error": f"SQL 执行失败: {exc.__class__.__name__}", "sql": sql}]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
