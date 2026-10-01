"""文档知识图谱 API: ACL 过滤的图谱查询 + 手动/批量重建。

身份沿用文档管理侧的显式传参范式(前端从当前员工带入 user_id/role/department),
在此构造 :class:`Principal` 交给 service 做权限裁剪。图谱未启用或 Neo4j 不可用时
一律返回空图(HTTP 200, ``enabled=false``), 不报错、不阻断页面。

两个重建端点都有闸门(原先谁都能调):
- 单篇重建限"文档所有者本人或 admin"。否则 ``reason=not_found``/``built`` 本身就在
  跨 ACL 探测别人的文档, 而且重建会重写该篇的图节点;
- 全量回填只开给 admin, 并且单飞: 每点一次都 ``create_task(rebuild_all())`` 等于
  按文档数×并发份数线性放大 LLM 成本, 还对同一批 KG_REL 边做重复 MERGE。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.kg import service
from app.schemas import Role
from app.security.acl import Principal, can_manage_document
from app.security.audit import get_audit_logger, new_trace_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kg", tags=["kg"])

# 后台回填任务的强引用: asyncio 只弱引用 task, 不持引用会被 GC 掉
_bg_tasks: set[asyncio.Task] = set()
# 全量回填的单飞引用(同一进程内最多一份在跑)
_rebuild_all_task: asyncio.Task | None = None


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
async def rebuild_document(
    doc_key: str, operator: str = "", role: str = "employee", department: str = ""
) -> dict:
    """手动重建单篇文档的图谱(同步执行, 用于抽取失败后的补建)。

    只允许文档所有者本人或 admin: 否则返回值本身就是跨 ACL 的存在性探测。
    """
    if not get_settings().doc_kg_enabled:
        raise HTTPException(status_code=400, detail="文档知识图谱未启用")
    principal = _principal(operator, role, department)
    if not principal.user_id or principal.user_id == "anonymous":
        raise HTTPException(status_code=400, detail="operator 必填且不能为 anonymous")
    from app.docs import service as docs_service

    doc = await docs_service.check_existing(doc_key)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在或已删除")
    if not can_manage_document(principal, doc.created_by or ""):
        get_audit_logger().log(
            new_trace_id(), "kg", "document_rebuild_denied",
            {"doc_key": doc_key, "operator": principal.user_id, "role": principal.role.value,
             "created_by": doc.created_by or ""},
        )
        raise HTTPException(status_code=403, detail="仅文档所有者或管理员可重建该篇图谱")
    result = await service.build_for_doc(doc_key)
    get_audit_logger().log(
        new_trace_id(), "kg", "document_rebuilt",
        {"doc_key": doc_key, "operator": principal.user_id, **result},
    )
    return result


@router.post("/rebuild-all")
async def rebuild_all_docs(
    operator: str = "", role: str = "employee", limit: int | None = Query(None, ge=1, le=5000)
) -> dict:
    """触发一次后台全量回填(仅 admin, 单飞; 立即返回受理状态, 不阻塞请求)。"""
    if not get_settings().doc_kg_enabled:
        raise HTTPException(status_code=400, detail="文档知识图谱未启用")
    principal = _principal(operator, role, "")
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="全量图谱回填仅管理员可触发")
    global _rebuild_all_task
    if _rebuild_all_task is not None and not _rebuild_all_task.done():
        # 已有任务在跑: 直接回绝而不是再开一份(重复全表逐篇 LLM 抽取)。
        return {
            "status": "already_running",
            "message": "已有全量回填任务在运行, 本次未重复启动",
        }
    task = asyncio.create_task(service.rebuild_all(limit_docs=limit))
    _rebuild_all_task = task
    _bg_tasks.add(task)

    def _finish(t: asyncio.Task) -> None:
        global _rebuild_all_task
        _bg_tasks.discard(t)
        if _rebuild_all_task is t:
            _rebuild_all_task = None

    task.add_done_callback(_finish)
    get_audit_logger().log(
        new_trace_id(), "kg", "rebuild_all_triggered",
        {"operator": principal.user_id, "limit": limit},
    )
    return {"status": "accepted", "message": "图谱回填已在后台启动, 稍后刷新查看"}
