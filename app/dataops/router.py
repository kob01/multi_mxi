"""数据变更审批台 REST(层 4 的人工审批流 + 层 6 的审计回查)。

身份口径沿用本仓既有欠债: 无 token, ``user_id``/``role`` 由请求自报(与
``app/docs/router.py`` 同一写法), 统一身份系统接入后一起替换。所以这一页是
**人工复核的界面**, 不是权限边界 —— 真正的边界在 :mod:`app.db.dataops` 里的角色名单、
"审批人 != 发起人"、RLS 与最小权限角色。

所有 dataops 调用都是同步的(与 FastMCP 工具共用同一套实现, 不分两条路径各写一遍),
在异步网关里走 threadpool, 避免把阻塞的 DB 调用压在事件循环上。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.db import dataops
from app.schemas import Role
from app.security.audit import get_audit_logger, new_trace_id

router = APIRouter(prefix="/api/dataops", tags=["dataops"])

_audit = get_audit_logger()


class Operator(BaseModel):
    """审批/驳回/回滚的入参: 操作者身份 + 一句理由。"""

    operator: str = Field(default="", description="操作者工号(必填, 不得为 anonymous)")
    role: str = Field(default="employee", description="操作者角色(自报, 与网关同口径)")
    note: str = Field(default="", description="审批备注或驳回理由")


def _require_operator(operator: str) -> str:
    user_id = (operator or "").strip()
    if not user_id or user_id == "anonymous":
        raise HTTPException(status_code=400, detail="operator 必填且不能为 anonymous")
    return user_id


def _normal_role(role: str) -> str:
    """非法角色回落到 employee(默认拒, 而不是当成管理员)。"""
    try:
        return Role((role or "employee").strip().lower()).value
    except ValueError:
        return Role.EMPLOYEE.value


@router.get("")
async def list_dataops(
    user_id: str = Query(default="", description="调用者工号"),
    role: str = Query(default="employee", description="调用者角色"),
    status: str = Query(default="", description="只看某个状态(留空=全部)"),
    limit: int = Query(default=30, ge=1, le=200),
) -> dict[str, Any]:
    """待办列表: 本人发起的全部可见; 有审批权的角色额外看到全部待办。"""
    return {
        "dataops": await run_in_threadpool(
            dataops.list_ops,
            user_id=(user_id or "").strip(),
            role=_normal_role(role),
            status=status or "",
            limit=limit,
        )
    }


@router.get("/audit")
async def recent_audit(
    operator: str = Query(default="", description="调用者工号"),
    role: str = Query(default="employee", description="调用者角色"),
    user_id: str = Query(default="", description="按被审计用户过滤(审批角色可用)"),
    limit: int = Query(default=30, ge=1, le=200),
) -> dict[str, Any]:
    """审计回查(层 6): 普通角色只看自己的记录, 审批角色可看全部。"""
    requester = _require_operator(operator)
    return {
        "records": await run_in_threadpool(
            dataops.recent_audit_records,
            actor_user_id=(user_id or "").strip(),
            requester=requester,
            requester_role=_normal_role(role),
            limit=limit,
        )
    }


@router.get("/{op_id}")
async def get_dataop(
    op_id: str,
    user_id: str = Query(default="", description="调用者工号"),
    role: str = Query(default="employee", description="调用者角色"),
) -> dict[str, Any]:
    """一个写计划的详情(含服务端生成的最终 SQL 与预演行数)。"""
    detail = await run_in_threadpool(
        dataops.get_op, op_id, user_id=user_id or "", role=_normal_role(role)
    )
    if detail.get("denied"):
        raise HTTPException(status_code=404, detail=detail.get("error", "写计划不存在"))
    return detail


@router.get("/{op_id}/images")
async def dataop_images(op_id: str) -> dict[str, Any]:
    """变更前镜像摘要(只回主键 + 本次要改的可写字段旧值)。"""
    return {"images": await run_in_threadpool(dataops.list_before_images, op_id)}


@router.post("/{op_id}/approve")
async def approve_dataop(op_id: str, body: Operator) -> dict[str, Any]:
    """审批通过并执行(审批人必须与发起人不同, 且角色在审批名单内)。"""
    operator = _require_operator(body.operator)
    trace_id = new_trace_id()
    result = await run_in_threadpool(
        dataops.approve_data_op,
        op_id,
        approver_user_id=operator,
        approver_role=_normal_role(body.role),
        note=body.note,
        trace_id=trace_id,
    )
    _audit.log(trace_id, "dataops_api", "dataop_approve_called",
               {"op_id": op_id, "operator": operator, "status": result.get("status")})
    if result.get("forbidden") or result.get("denied"):
        raise HTTPException(status_code=403, detail=result.get("error", "审批被拒"))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/{op_id}/reject")
async def reject_dataop(op_id: str, body: Operator) -> dict[str, Any]:
    """审批驳回: 只改状态, 不碰数据。"""
    operator = _require_operator(body.operator)
    trace_id = new_trace_id()
    result = await run_in_threadpool(
        dataops.reject_data_op,
        op_id,
        approver_user_id=operator,
        approver_role=_normal_role(body.role),
        note=body.note,
        trace_id=trace_id,
    )
    _audit.log(trace_id, "dataops_api", "dataop_reject_called",
               {"op_id": op_id, "operator": operator, "status": result.get("status")})
    if result.get("forbidden") or result.get("denied"):
        raise HTTPException(status_code=403, detail=result.get("error", "驳回被拒"))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/{op_id}/rollback")
async def rollback_dataop(op_id: str, body: Operator) -> dict[str, Any]:
    """按变更前镜像回滚(只允许有写权限的角色, 且计划必须是 EXECUTED)。"""
    operator = _require_operator(body.operator)
    trace_id = new_trace_id()
    result = await run_in_threadpool(
        dataops.rollback_data_op,
        op_id,
        user_id=operator,
        role=_normal_role(body.role),
        trace_id=trace_id,
    )
    _audit.log(trace_id, "dataops_api", "dataop_rollback_called",
               {"op_id": op_id, "operator": operator, "status": result.get("status")})
    if result.get("forbidden") or result.get("denied"):
        raise HTTPException(status_code=403, detail=result.get("error", "回滚被拒"))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result
