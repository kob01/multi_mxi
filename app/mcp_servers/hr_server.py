"""HR ticket-system MCP Server (FastMCP, streamable-http transport).

Wraps the enterprise HR backend stored in MySQL (hr_employees / hr_tickets /
hr_leave_records) as standard MCP tools so any MCP-compatible client can call
them. Also exposes a read-only Text2SQL tool (execute_sql) with a hard
table whitelist.

Run:
    python -m app.mcp_servers.hr_server          # serves http://0.0.0.0:8001/mcp
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import sync as dbsync
from app.db.models import Employee, HRTicket
from app.db.sql_guard import SQLGuardError

mcp = FastMCP("hr-ticket-system", host="0.0.0.0", port=8001)

TICKET_CATEGORIES = {"入职", "离职", "考勤", "薪酬", "证明开具", "其他"}
# Text2SQL 表白名单 (硬控制, 见 app/db/sql_guard.py)
ALLOWED_TABLES = {"hr_employees", "hr_tickets", "hr_leave_records"}


def _next_ticket_no(session: Session) -> str:
    max_no = session.execute(
        text("SELECT COALESCE(MAX(CAST(SUBSTRING(ticket_no, 3) AS SIGNED)), 999) FROM hr_tickets")
    ).scalar_one()
    return f"HR{int(max_no) + 1}"


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
def create_hr_ticket(user_id: str, category: str, title: str, description: str) -> dict[str, Any]:
    """Create an HR service ticket.

    Args:
        user_id: Employee ID of the requester.
        category: One of 入职/离职/考勤/薪酬/证明开具/其他.
        title: Short ticket title.
        description: Detailed request description.

    Returns:
        The created ticket record including ticket_no and status.
    """
    if category not in TICKET_CATEGORIES:
        return {"error": f"非法工单类别: {category}; 可选: {sorted(TICKET_CATEGORIES)}"}
    with Session(dbsync.get_sync_engine()) as session:
        ticket = HRTicket(
            ticket_no=_next_ticket_no(session),
            emp_id=user_id,
            category=category,
            title=title,
            description=description,
            status="OPEN",
            created_at=datetime.now(),
        )
        session.add(ticket)
        session.commit()
        return _ticket_dict(ticket)


@mcp.tool()
def query_hr_ticket(ticket_no: str) -> dict[str, Any]:
    """Query an HR ticket by its ticket number.

    Args:
        ticket_no: Ticket number, e.g. HR1000.

    Returns:
        Ticket record, or an error payload if not found.
    """
    with Session(dbsync.get_sync_engine()) as session:
        ticket = session.get(HRTicket, ticket_no)
        if ticket is None:
            return {"error": f"ticket {ticket_no} not found"}
        return _ticket_dict(ticket)


@mcp.tool()
def list_hr_tickets(user_id: str) -> list[dict[str, Any]]:
    """List all HR tickets submitted by a given employee.

    Args:
        user_id: Employee ID.

    Returns:
        List of ticket records (may be empty).
    """
    with Session(dbsync.get_sync_engine()) as session:
        tickets = session.scalars(
            select(HRTicket).where(HRTicket.emp_id == user_id).order_by(HRTicket.created_at.desc())
        ).all()
        return [_ticket_dict(t) for t in tickets]


@mcp.tool()
def cancel_hr_ticket(ticket_no: str) -> dict[str, Any]:
    """Cancel an OPEN HR ticket.

    Args:
        ticket_no: Ticket number to cancel.

    Returns:
        Updated ticket record or error payload.
    """
    with Session(dbsync.get_sync_engine()) as session:
        ticket = session.get(HRTicket, ticket_no)
        if ticket is None:
            return {"error": f"ticket {ticket_no} not found"}
        if ticket.status != "OPEN":
            return {"error": f"ticket {ticket_no} is {ticket.status}, cannot cancel"}
        ticket.status = "CANCELLED"
        session.commit()
        return _ticket_dict(ticket)


@mcp.tool()
def get_leave_balance(user_id: str) -> dict[str, Any]:
    """Get annual-leave balance of an employee (from hr_employees table).

    Args:
        user_id: Employee ID.

    Returns:
        Balance info: total / used / remaining days.
    """
    with Session(dbsync.get_sync_engine()) as session:
        emp = session.get(Employee, user_id)
        if emp is None:
            return {"error": f"employee {user_id} not found"}
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
        sql: A single read-only MySQL SELECT statement against whitelisted tables.

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
