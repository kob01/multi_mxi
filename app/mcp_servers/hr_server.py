"""HR ticket-system MCP Server (FastMCP, streamable-http transport).

Wraps the enterprise HR backend stored in PostgreSQL (hr_employees /
hr_tickets / hr_leave_records) as standard MCP tools so any MCP-compatible
client can call them. Also exposes a read-only Text2SQL tool (execute_sql)
with a hard table whitelist.

归属校验(每个单据类工具都要过): ``caller_user_id`` / ``caller_role`` 由助手网关
在调用前注入(见 app/security/caller.py), 工具只信这一份, 不信 LLM 传的工号:
员工只能查/改自己名下的工单与假期, manager/hr/finance/admin 可跨人。因此直连
本 server 而不带调用者的调用会被拒(默认拒, 而不是当成全权限)。

Run:
    python -m app.mcp_servers.hr_server          # serves http://0.0.0.0:8001/mcp
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import sync as dbsync
from app.db.models import Employee, HRTicket
from app.db.sequences import next_numbered
from app.db.sql_guard import SQLGuardError
from app.security.caller import (
    guard_owner,
    guard_target_user,
    resolve_caller,
)

mcp = FastMCP("hr-ticket-system", host="0.0.0.0", port=8001)

TICKET_CATEGORIES = {"入职", "离职", "考勤", "薪酬", "证明开具", "其他"}
# Text2SQL 表白名单 (硬控制, 见 app/db/sql_guard.py)
ALLOWED_TABLES = {"hr_employees", "hr_tickets", "hr_leave_records"}


def _next_ticket_no(session: Session) -> str:
    """取工单号: 走序列而非 MAX+1(并发不重号, 号段也不那么可预测)。"""
    return next_numbered(
        session,
        sequence="hr_ticket_no_seq",
        prefix="HR",
        table="hr_tickets",
        column="ticket_no",
        start=1000,
    )


def _ticket_dict(t: HRTicket) -> dict[str, Any]:
    return {
        "ticket_no": t.ticket_no,
        "user_id": t.emp_id,
        "category": t.category,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "created_at": t.created_at.isoformat(timespec="seconds"),
    }


@mcp.tool()
def create_hr_ticket(
    category: str,
    title: str,
    description: str,
    user_id: str = "",
    caller_user_id: str = "",
    caller_role: str = "",
) -> dict[str, Any]:
    """Create an HR service ticket.

    默认为"当前登录者自己"开单; 要代他人开单需要管理角色(manager/hr/finance/admin)。

    Args:
        category: One of 入职/离职/考勤/薪酬/证明开具/其他.
        title: Short ticket title.
        description: Detailed request description.
        user_id: 被开单员工工号; 留空即本人。
        caller_user_id: 调用者工号(网关注入, 请勿自行填写)。
        caller_role: 调用者角色(网关注入, 请勿自行填写)。

    Returns:
        The created ticket record including ticket_no and status, or {error}.
    """
    if category not in TICKET_CATEGORIES:
        return {"error": f"非法工单类别: {category}; 可选: {sorted(TICKET_CATEGORIES)}"}
    effective, denial = guard_target_user(
        resolve_caller(caller_user_id, caller_role), user_id, what="员工开工单"
    )
    if denial is not None:
        return denial
    with Session(dbsync.get_sync_engine()) as session:
        try:
            ticket = HRTicket(
                ticket_no=_next_ticket_no(session),
                emp_id=effective,
                category=category,
                title=title,
                description=description,
                status="OPEN",
                # timestamptz 列: 必须传带时区的值
                created_at=datetime.now(timezone.utc),
            )
            session.add(ticket)
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            return {"error": f"工单写入冲突({exc.__class__.__name__}), 请重试一次"}
        except SQLAlchemyError as exc:
            session.rollback()
            return {"error": f"工单创建失败({exc.__class__.__name__}), 请稍后重试"}
        return _ticket_dict(ticket)


@mcp.tool()
def query_hr_ticket(
    ticket_no: str, caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """Query an HR ticket by its ticket number.

    Args:
        ticket_no: Ticket number, e.g. HR1000.
        caller_user_id: 调用者工号(网关注入, 请勿自行填写)。
        caller_role: 调用者角色(网关注入, 请勿自行填写)。

    Returns:
        Ticket record, or an error payload if not found / not yours.
    """
    caller = resolve_caller(caller_user_id, caller_role)
    with Session(dbsync.get_sync_engine()) as session:
        ticket = session.get(HRTicket, ticket_no)
        if ticket is None:
            return {"error": f"ticket {ticket_no} not found"}
        denial = guard_owner(caller, ticket.emp_id, what="工单")
        if denial is not None:
            return denial
        return _ticket_dict(ticket)


@mcp.tool()
def list_hr_tickets(
    user_id: str = "", caller_user_id: str = "", caller_role: str = ""
) -> list[dict[str, Any]] | dict[str, Any]:
    """List HR tickets submitted by a given employee (默认只列本人)。

    Args:
        user_id: 目标员工工号; 留空自默认列自己的工单。
        caller_user_id: 调用者工号(网关注入, 请勿自行填写)。
        caller_role: 调用者角色(网关注入, 请勿自行填写)。

    Returns:
        List of ticket records (may be empty), or {error} on 越权。
    """
    effective, denial = guard_target_user(
        resolve_caller(caller_user_id, caller_role), user_id, what="工单"
    )
    if denial is not None:
        return denial
    with Session(dbsync.get_sync_engine()) as session:
        tickets = session.scalars(
            select(HRTicket).where(HRTicket.emp_id == effective).order_by(HRTicket.created_at.desc())
        ).all()
        return [_ticket_dict(t) for t in tickets]


@mcp.tool()
def cancel_hr_ticket(
    ticket_no: str, caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """Cancel an OPEN HR ticket (只能取消自己的, 管理角色除外)。

    Args:
        ticket_no: Ticket number to cancel.
        caller_user_id: 调用者工号(网关注入, 请勿自行填写)。
        caller_role: 调用者角色(网关注入, 请勿自行填写)。

    Returns:
        Updated ticket record or error payload.
    """
    caller = resolve_caller(caller_user_id, caller_role)
    with Session(dbsync.get_sync_engine()) as session:
        try:
            ticket = session.get(HRTicket, ticket_no)
            if ticket is None:
                return {"error": f"ticket {ticket_no} not found"}
            denial = guard_owner(caller, ticket.emp_id, what="工单")
            if denial is not None:
                return denial
            if ticket.status != "OPEN":
                return {"error": f"ticket {ticket_no} is {ticket.status}, cannot cancel"}
            ticket.status = "CANCELLED"
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            return {"error": f"工单取消失败({exc.__class__.__name__}), 请稍后重试"}
        return _ticket_dict(ticket)


@mcp.tool()
def get_leave_balance(
    user_id: str = "", caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """Get annual-leave balance (from hr_employees table); 默认查本人。

    Args:
        user_id: 目标员工工号; 留空即本人。
        caller_user_id: 调用者工号(网关注入, 请勿自行填写)。
        caller_role: 调用者角色(网关注入, 请勿自行填写)。

    Returns:
        Balance info: total / used / remaining days, or {error}。
    """
    effective, denial = guard_target_user(
        resolve_caller(caller_user_id, caller_role), user_id, what="假期余额"
    )
    if denial is not None:
        return denial
    with Session(dbsync.get_sync_engine()) as session:
        emp = session.get(Employee, effective)
        if emp is None:
            return {"error": f"employee {effective} not found"}
        return {
            "user_id": emp.emp_id,
            "name": emp.name,
            "annual_leave_total": emp.annual_leave_total,
            "used": emp.annual_leave_used,
            "remaining": emp.annual_leave_total - emp.annual_leave_used,
        }


@mcp.tool()
def execute_sql(sql: str) -> list[dict[str, Any]]:
    """Text2SQL: 在 HR 业务库上执行一条只读 SELECT 查询并返回结果。

    仅可查询以下表: hr_employees(员工主数据, 含年假额度), hr_tickets(HR 工单),
    hr_leave_records(请假记录)。仅允许单条 SELECT; 禁止写操作;
    结果最多返回 50 行。优先使用专用工具(create_hr_ticket 等)完成业务操作,
    本工具用于统计/明细等灵活查询。

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


@mcp.tool()
def lookup_employee_by_name(
    name: str, caller_user_id: str = "", caller_role: str = ""
) -> dict[str, Any]:
    """Look up employee(s) by name and return their user_id (emp_id).

    Use this when the user refers to a target person by NAME only (no emp_id),
    before calling tools that require a user_id. If several employees share
    the same name, the result carries needs_selection=True plus a candidates
    list — present the candidates (name + user_id + department) to the user
    and ask them to pick one; do NOT guess an emp_id by yourself.

    Args:
        name: Employee name, e.g. 张三. Exact match first; falls back to a
            substring match when no exact match exists.
        caller_user_id: 网关注入的调用者工号(本工具只读, 缺身份不拒)。
        caller_role: 网关注入的调用者角色, 决定能否看到职位字段。

    Returns:
        Single match: {user_id, name, department, status} (管理角色另带 position)。
        Multiple matches: {needs_selection: True, candidates: [...]}.
        No match: {error: ...}.
    """
    name = name.strip()
    if not name:
        return {"error": "员工姓名不能为空"}
    # 层 0(收回跨域凭证): 本工具原先是各 agent 进程内直连库实现的
    # (app/agents/common_tools.py), 于是每个 agent 容器都得持有一份能读写全部业务表
    # 的凭据。基础解析能力归到数据属域(HR MCP)由服务端代做, agent 进程只讲 MCP。
    caller = resolve_caller(user_id=caller_user_id, role=caller_role)
    with Session(dbsync.get_sync_engine()) as session:
        stmt = select(Employee).where(Employee.name == name).order_by(Employee.emp_id)
        emps = session.scalars(stmt).all()
        if not emps:  # 精确匹配失败 -> 姓名包含匹配 (如"小王"匹配"王小明")
            emps = session.scalars(
                select(Employee).where(Employee.name.contains(name)).order_by(Employee.emp_id)
            ).all()
        if not emps:
            return {"error": f"未找到姓名包含 \"{name}\" 的员工"}
        candidates = [
            {
                "user_id": e.emp_id,
                "name": e.name,
                "department": e.department,
                "status": e.status,
            }
            for e in emps
        ]
        if caller is None or caller.is_privileged:
            for item, emp in zip(candidates, emps):
                item["position"] = emp.position
        if len(candidates) == 1:
            return candidates[0]
        # 同名多人: 返回结构化候选列表, 由前端渲染选择器(姓名+工号对应关系),
        # 或由 LLM 以编号列表形式请用户选择。
        return {
            "needs_selection": True,
            "message": f"找到 {len(candidates)} 位姓名包含 \"{name}\" 的员工, 请选择目标员工",
            "candidates": candidates,
        }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
