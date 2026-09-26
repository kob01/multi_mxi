"""文档知识图谱 API: ACL 过滤的图谱查询 + 手动/批量重建。

身份沿用文档管理侧的显式传参范式(前端从当前员工带入 user_id/role/department),
在此构造 :class:`Principal` 交给 service 做权限裁剪。图谱未启用或 Neo4j 不可用时
一律返回空图(HTTP 200, ``enabled=false``), 不报错、不阻断页面。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.kg import service
from app.schemas import Role
from app.security.acl import Principal
from app.security.audit import get_audit_logger, new_trace_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kg", tags=["kg"])

# 后台回填任务的强引用: asyncio 只弱引用 task, 不持引用会被 GC 掉
_bg_tasks: set[asyncio.Task] = set()


def _principal(user_id: str, role: str, department: str) -> Principal:
    try:
        role_enum = Role(role)
    except ValueError:
        role_enum = Role.EMPLOYEE
    return Principal(user_id=user_id or "", department=department or "", role=role_enum)


@router.get("/graph")
async def get_graph(
    user_id: str = Query("", description="当前用户工号(ACL 判定)"),
    role: str = Query("employee", description="当前角色(ACL 判定)"),
    department: str = Query("", description="当前部门(ACL 判定)"),
    focus: str | None = Query(None, description="聚焦展开的文档 doc_key"),
    hops: int | None = Query(None, ge=1, le=4, description="邻域跳数"),
    limit: int | None = Query(None, ge=1, le=1000, description="节点上限"),
) -> dict:
    """返回按当前用户 ACL 过滤后的文档知识图谱子图。"""
    principal = _principal(user_id, role, department)
    return await service.get_graph(principal, focus=focus, hops=hops, limit=limit)


@router.post("/documents/{doc_key}/rebuild")
async def rebuild_document(doc_key: str, operator: str = "anonymous") -> dict:
    """手动重建单篇文档的图谱(同步执行, 用于抽取失败后的补建)。"""
    if not get_settings().doc_kg_enabled:
        raise HTTPException(status_code=400, detail="文档知识图谱未启用")
    result = await service.build_for_doc(doc_key)
    get_audit_logger().log(
        new_trace_id(), "kg", "document_rebuilt",
        {"doc_key": doc_key, "operator": operator, **result},
    )
    return result


@router.post("/rebuild-all")
async def rebuild_all_docs(operator: str = "anonymous") -> dict:
    """触发一次后台全量回填(立即返回受理状态, 不阻塞请求)。"""
    if not get_settings().doc_kg_enabled:
        raise HTTPException(status_code=400, detail="文档知识图谱未启用")
    task = asyncio.create_task(service.rebuild_all())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    get_audit_logger().log(
        new_trace_id(), "kg", "rebuild_all_triggered", {"operator": operator},
    )
    return {"status": "accepted", "message": "图谱回填已在后台启动, 稍后刷新查看"}
