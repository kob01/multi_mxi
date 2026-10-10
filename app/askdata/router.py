"""智能问数自服务 REST: 指标看板、反馈事件、badcase 待办、评测集与语义层快照。

身份口径沿用本仓既有欠债(与 ``app/dataops/router.py`` 同一写法): ``user_id``/``role``
由请求自报, 统一身份系统接入后一起替换。因此这里的权限判定是**界面级可见性**,
真正的数据边界仍在数据库 RLS 与最小权限角色上。

为什么这些端点放在网关而不是 askdata MCP server: 它们是"治理面"(看统计、下反馈、
管评测集), 调用方是页面与运维, 不是模型 —— 让模型能改评测集等于让它给自己出考卷。

DAO 是同步实现(与 MCP 工具共用一份), 在异步路由里一律 ``run_in_threadpool``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.askdata import attribution, store
from app.config import get_settings
from app.security.audit import get_audit_logger

router = APIRouter(prefix="/api/askdata", tags=["askdata"])

_audit = get_audit_logger()


class FeedbackIn(BaseModel):
    """一条反馈事件: 点赞 / 点踩 / 用户改写。"""

    query_id: str = Field(description="``log_query`` 返回的问数记录号")
    kind: str = Field(default="down", description="up / down / rewritten")
    user_id: str = Field(default="", description="反馈人(自报, 用于限定只能反馈自己问过的)")
    attribution_hint: str = Field(default="", description="前端可选: 用户自己认为错在哪一段")
    corrected_dsl: dict[str, Any] | None = Field(default=None, description="改写后的正确 DSL")
    note: str = Field(default="", description="补一句话说明")


class GoldenIn(BaseModel):
    """评测集用例(新增或按 case_id 更新)。"""

    question: str
    dsl: dict[str, Any] | None = None
    expected_sql: str = ""
    expected_result: dict[str, Any] | None = None
    tags: str = ""
    note: str = ""
    created_by: str = ""
    enabled: bool = True
    case_id: str = ""


def _privileged(role: str) -> bool:
    """能不能看别人的问数与治理面: 沿用"能跨全员看数的角色"这一份名单。

    刻意不新开关一个角色配置: 问数留痕里就带着别人的问题与数字, "可以看全员数据"
    与"可以看全员问数记录"在本仓是同一件事, 再开一个名单只会漂移出两个不一致的权限。
    """
    raw = (get_settings().analytics_all_dept_roles or "").lower()
    return (role or "").strip().lower() in {p.strip() for p in raw.split(",") if p.strip()}


@router.get("/stats")
async def stats(
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    """指标看板: 一次通过率 / 澄清触发率 / 降级率 / 点踩率 / P95 延迟 / 最近 EX。"""
    _require_privileged(role, operator, "问数看板")
    return await run_in_threadpool(store.stats, days=days)


@router.get("/queries")
async def list_queries(
    user_id: str = Query(default="", description="按提问者过滤"),
    role: str = Query(default="employee", description="调用者角色"),
    verdict: str = Query(default="", description="ok / clarify / denied / fail / degraded"),
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=30, ge=1, le=200),
) -> dict[str, Any]:
    """回查问数记录: 普通角色只看自己问过的那几条。"""
    caller = (user_id or "").strip()
    if not _privileged(role) and not caller:
        raise HTTPException(status_code=400, detail="缺少 user_id")
    return {
        "queries": await run_in_threadpool(
            store.list_queries, user_id=caller, verdict=verdict, days=days, limit=limit
        )
    }


@router.post("/feedback")
async def submit_feedback(req: FeedbackIn) -> dict[str, Any]:
    """收下一条反馈并触发五类归因(处置只产出**草案**, 生效要过人)。"""
    row = await run_in_threadpool(store.get_query, req.query_id)
    if not row:
        raise HTTPException(status_code=404, detail="没找到这条问数记录")
    caller = (req.user_id or "").strip()
    if caller and str(row.get("user_id") or "") not in (caller, ""):
        raise HTTPException(status_code=403, detail="只能对自己问过的记录给反馈")
    updated = await run_in_threadpool(
        store.set_feedback, req.query_id, req.kind,
        attribution=req.attribution_hint, corrected_dsl=req.corrected_dsl,
        note=req.note, user_id=caller,
    )
    if updated.get("error"):
        raise HTTPException(status_code=400, detail=updated["error"])
    category, disposition = attribution.attribute({**row, "feedback": req.kind})
    routed = await run_in_threadpool(
        attribution.apply_disposition, {**row, "feedback": req.kind},
        category=category, disposition=disposition, corrected_dsl=req.corrected_dsl,
    )
    _audit.log(
        str(row.get("trace_id") or ""), "assistant", "askdata_feedback",
        {"query_id": req.query_id, "kind": req.kind, "category": category,
         "disposition": disposition, "written": routed.get("written", ""),
         "user_id": caller or "anonymous"},
    )
    return {
        "status": "ok", "query_id": req.query_id, "feedback": req.kind,
        "category": category, "disposition": disposition,
        "draft": routed.get("written", ""),
        "note": "归因只产出草案(补词/评测集均为未生效状态), 需要人工核对后才会计入口径",
    }


@router.get("/badcases")
async def list_badcases(
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
    days: int = Query(default=14, ge=1, le=90),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """待标注清单: 被点踩/改写与失败的问数, 带上归因类别与处置建议。

    这就是"SFT 回流"在本期的形态 —— 只出一张需要人判的清单, 不做自动训练。
    """
    _require_privileged(role, operator, "badcase 清单")
    rows = await run_in_threadpool(
        store.list_queries, verdict="", feedback="down", days=days, limit=limit
    )
    failed = await run_in_threadpool(store.list_queries, verdict="fail", days=days, limit=limit)
    seen = {r.get("query_id") for r in rows}
    items = rows + [r for r in failed if r.get("query_id") not in seen]
    out = []
    for row in items:
        category, disposition = attribution.attribute(row)
        out.append({**row, "category": category, "disposition": disposition})
    return {"badcases": out, "count": len(out), "window_days": days}


@router.get("/golden")
async def list_golden(
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
    include_drafts: bool = Query(default=False, description="是否含未启用的草案"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    """评测集列表(治理面: 只有能看全员数据的角色可列)。"""
    _require_privileged(role, operator, "评测集")
    return {
        "cases": await run_in_threadpool(store.list_golden, enabled_only=not include_drafts, limit=limit)
    }


@router.post("/golden")
async def add_golden(req: GoldenIn, role: str = Query(default="employee")) -> dict[str, Any]:
    """新增/更新一条评测用例(草案应由归因路由产生, 这里给页面/脚本手工补录用)。"""
    _require_privileged(role, req.created_by, "评测集写入")
    out = await run_in_threadpool(
        store.upsert_golden,
        question=req.question, dsl=req.dsl, expected_sql=req.expected_sql,
        expected_result=req.expected_result, tags=req.tags, note=req.note,
        created_by=req.created_by, enabled=req.enabled, case_id=req.case_id,
    )
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out["error"])
    _audit.log("", "assistant", "askdata_golden_upsert",
               {"case_id": out.get("case_id"), "enabled": out.get("enabled"),
                "operator": req.created_by or "anonymous"})
    return out


@router.get("/semantic")
async def semantic_snapshot(
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
    refresh: bool = Query(default=False, description="强制重读语义层快照"),
) -> dict[str, Any]:
    """语义层快照概览(排查用): 现在用的是库里版本还是内置兜底集。"""
    _require_privileged(role, operator, "语义层快照")
    from app.semantic import registry

    if refresh:
        registry.invalidate()
    catalog = await run_in_threadpool(registry.get_catalog)
    return {
        "catalog": catalog.to_payload(),
        "golden_enabled": await run_in_threadpool(store.golden_count),
        "metrics": catalog.render_metrics(),
        "dimensions": catalog.render_dimensions(),
    }


def _require_privileged(role: str, operator: str, what: str) -> None:
    """只判角色; ``operator`` 存在的意义是审计里能写下"谁看了这一页"(写不写入由调用方定)。"""
    _ = (operator or "").strip()
    if not _privileged(role):
        raise HTTPException(status_code=403, detail=f"角色 {role or 'employee'} 无权查看{what}")
