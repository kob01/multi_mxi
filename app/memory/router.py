"""个人记忆的自服务接口 (供前端"我的记忆"页读取/清理)。

端点:
    GET  /api/memory/overview        画像 + 各桶明细 + 图谱 + 计数概览
    GET  /api/memory/graph           单独取个人图谱子图(图谱页刷新用)
    DELETE /api/memory/items/{id}    删除一条记忆(仅本人)
    DELETE /api/memory/bucket/{kind} 清空一个桶(仅本人; profile 桶整行重置)
    POST /api/memory/reflect         手动触发一次"情节 -> 个人知识"蒸馏

个人数据必须自服务: 本系统没有 token, 身份由 ``user_id`` + ``operator`` 两个查询
参数声明(与文档管理接口同一约定), 因此这里能做的只有"operator 必须等于
user_id"这道校验 —— 它挡住的是误操作与前端串号, 不是恶意伪造(那需要真正的认证,
属于本系统尚未引入的能力)。所有写操作都落审计。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app.db.session import db_available
from app.memory import graph_store
from app.memory.personal import get_personal_agent
from app.memory.taxonomy import CLEARABLE_KINDS
from app.security.audit import get_audit_logger, new_trace_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _require_self(user_id: str, operator: str) -> None:
    """个人记忆只允许本人读写; 身份不符按 403 处理。"""
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    if operator and operator != user_id:
        raise HTTPException(status_code=403, detail="个人记忆仅支持本人访问")


@router.get("/overview")
async def memory_overview(user_id: str, operator: str = "") -> dict:
    """某用户的个人记忆全景(画像/偏好/习惯/情节/知识/图谱)。"""
    _require_self(user_id, operator)
    return await get_personal_agent().overview(user_id)


@router.get("/graph")
async def memory_graph(user_id: str, operator: str = "", limit: int = 60) -> dict:
    """以用户为锚点的一跳子图; Neo4j 不可用时返回空结构(200)。"""
    _require_self(user_id, operator)
    return await graph_store.user_subgraph(user_id, limit=limit)


@router.delete("/items/{item_id}")
async def delete_memory_item(item_id: int, user_id: str, operator: str = "") -> dict:
    """删除一条记忆(按 id + user_id 双条件, 删不到就是不存在)。"""
    _require_self(user_id, operator)
    if not db_available():
        raise HTTPException(status_code=502, detail="数据库不可用, 无法删除记忆")
    trace_id = new_trace_id()
    deleted = await get_personal_agent().delete(user_id, item_id)
    get_audit_logger().log(
        trace_id, "memory", "memory_item_deleted",
        {"user_id": user_id, "operator": operator or user_id, "item_id": item_id, "deleted": deleted},
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="记忆不存在或已删除")
    return {"status": "deleted", "item_id": item_id}


@router.delete("/bucket/{bucket}")
async def clear_memory_bucket(bucket: str, user_id: str, operator: str = "") -> dict:
    """清空一个桶; ``profile`` 走画像整行重置, 其余(含遗留 ``fact``)按 kind 删除。"""
    _require_self(user_id, operator)
    if bucket not in CLEARABLE_KINDS:
        valid = "/".join(CLEARABLE_KINDS)
        raise HTTPException(status_code=400, detail=f"未知记忆桶 {bucket}, 可选: {valid}")
    if not db_available():
        raise HTTPException(status_code=502, detail="数据库不可用, 无法清空记忆")
    trace_id = new_trace_id()
    cleared = await get_personal_agent().clear(user_id, bucket)
    get_audit_logger().log(
        trace_id, "memory", "memory_bucket_cleared",
        {"user_id": user_id, "operator": operator or user_id, "bucket": bucket, "cleared": cleared},
    )
    return {"status": "cleared", "bucket": bucket, "cleared": cleared}


@router.post("/reflect")
async def reflect_memories(user_id: str, operator: str = "") -> dict:
    """手动触发一次情节蒸馏(前端"整理记忆"按钮); force 跳过门槛判定。"""
    _require_self(user_id, operator)
    if not db_available():
        raise HTTPException(status_code=502, detail="数据库不可用, 无法整理记忆")
    trace_id = new_trace_id()
    added = await get_personal_agent().reflect(user_id, force=True)
    get_audit_logger().log(
        trace_id, "memory", "memory_reflected",
        {"user_id": user_id, "operator": operator or user_id, "knowledge_added": added},
    )
    return {"status": "ok", "knowledge_added": added}
