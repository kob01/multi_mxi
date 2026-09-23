"""Shared tools injected into the entry agents (orchestrator + specialists).

这些工具不属于任何业务域 MCP server, 而是跨域的基础解析能力 (如
姓名->工号), 由入口层各 ReAct 循环统一注入, 避免在多个 MCP server
中重复实现。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import sync as dbsync
from app.db.models import Employee


@tool
def lookup_employee_by_name(name: str) -> dict[str, Any]:
    """Look up employee(s) by name and return their user_id (emp_id).

    Use this when the user refers to a target person by NAME only (no emp_id),
    before calling tools that require a user_id. If several employees share
    the same name, the result carries needs_selection=True plus a candidates
    list — present the candidates (name + user_id + department) to the user
    and ask them to pick one; do NOT guess an emp_id by yourself.

    Args:
        name: Employee name, e.g. 张三. Exact match first; falls back to a
            substring match when no exact match exists.

    Returns:
        Single match: {user_id, name, department, position, status}.
        Multiple matches: {needs_selection: True, candidates: [...]}.
        No match: {error: ...}.
    """
    name = name.strip()
    if not name:
        return {"error": "员工姓名不能为空"}
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
                "position": e.position,
                "status": e.status,
            }
            for e in emps
        ]
        if len(candidates) == 1:
            return candidates[0]
        # 同名多人: 返回结构化候选列表, 由前端渲染选择器(姓名+工号对应关系),
        # 或由 LLM 以编号列表形式请用户选择。
        return {
            "needs_selection": True,
            "message": f"找到 {len(candidates)} 位姓名包含 \"{name}\" 的员工, 请选择目标员工",
            "candidates": candidates,
        }
