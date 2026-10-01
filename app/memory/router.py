"""个人记忆的自服务接口 (供前端"我的记忆"页读取/清理)。

端点:
    GET  /api/memory/overview        画像 + 各桶明细 + 图谱 + 计数概览
    GET  /api/memory/graph           单独取个人图谱子图(图谱页刷新用)
    DELETE /api/memory/items/{id}    删除一条记忆(仅本人)
    DELETE /api/memory/bucket/{kind} 清空一个桶(仅本人; profile 桶整行重置)
    POST /api/memory/reflect         手动"整理记忆": 偏好/习惯语义归并(情节蒸馏已下线)

个人数据必须自服务: 本系统没有 token, 身份由 ``user_id`` + ``operator`` 两个查询
参数声明(与文档管理接口同一约定)。因此 ``operator`` **必填**且必须等于 ``user_id``——
原先写成 ``if operator and operator != user_id`` 等于把这道校验做成了"可选":
不传 operator 就能读/清任何人的长期记忆。现在缺 operator 直接 400, 不符 403。
仍需记住本层挡住的是误操作、前端串号与省略参数绕过, 不是恶意伪造一个已知工号
(那需要真正的认证, 属于本系统尚未引入的能力; 届时只换参数来源, 判定不用改)。
所有写操作都落审计。
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
    """个人记忆只允许本人读写: operator 必填且必须等于 user_id。

    两个参数都不能省: 省掉任何一个就无法确认"调用者到底是谁", 而记忆是最怕
    被他人读取的数据(画像/情节/健康类属性都在里面)。
    """
    if not user_id or not operator:
        raise HTTPException(status_code=400, detail="user_id/operator 均为必填")
    if operator != user_id:
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
        {"user_id": user_id, "operator": operator, "item_id": item_id, "deleted": deleted},
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
        {"user_id": user_id, "operator": operator, "bucket": bucket, "cleared": cleared},
    )
    return {"status": "cleared", "bucket": bucket, "cleared": cleared}


@router.post("/reflect")
async def reflect_memories(user_id: str, operator: str = "") -> dict:
    """手动整理记忆(前端"整理记忆"按钮): 只做偏好/习惯语义归并。

    路径沿用 ``/reflect`` 是为了不断老脚本/前端; 情节 -> 知识的蒸馏已下线
    (知识桶只由显式"记一下"指令写入), 归并专门清理"同一件事不同说法"累积的重复行。
    """
    _require_self(user_id, operator)
    if not db_available():
        raise HTTPException(status_code=502, detail="数据库不可用, 无法整理记忆")
    trace_id = new_trace_id()
    result = await get_personal_agent().tidy(user_id)
    get_audit_logger().log(
        trace_id, "memory", "memory_tidied",
        {"user_id": user_id, "operator": operator, "merged": result.get("merged", 0)},
    )
    return {"status": "ok", **result}
