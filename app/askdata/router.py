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
from app.observability import logsink
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
    slots: dict[str, Any] | None = None
    expected_sql: str = ""
    expected_result: dict[str, Any] | None = None
    tags: str = ""
    note: str = ""
    created_by: str = ""
    enabled: bool = True
    case_id: str = ""
    difficulty: str = "easy"
    intent: str = ""


class EventIn(BaseModel):
    """一条结构化反馈事件(方案 §4⑥ 的事件表)。"""

    query_id: str = Field(default="", description="对应那一轮留痕")
    trace_id: str = Field(default="", description="链路号(没 query_id 时靠它回查)")
    session_id: str = Field(default="", description="会话号")
    user_id: str = Field(default="", description="事件人(自报)")
    event_type: str = Field(description="like/dislike/edit_query/edit_dsl/copy/drill/ticket/noanswer")
    payload: dict[str, Any] | None = Field(default=None, description="事件载荷(edit_dsl 存改后的 DSL)")
    stage: str = Field(default="", description="前端可选: 当时卡在哪个阶段")
    attribute: bool = Field(default=True, description="是否顺手跑一次归因并产出草案")


class TicketIn(BaseModel):
    """人工工单(页面侧主动开单/处理)。"""

    question: str = ""
    reason: str = Field(default="user_request", description="开单原因码")
    query_id: str = ""
    trace_id: str = ""
    dsl: dict[str, Any] | None = None
    semver: str = ""
    requested_by: str = ""
    assigned_to: str = ""
    status: str = ""
    resolution: str = ""


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


@router.get("/queries/{query_id}")
async def query_detail(
    query_id: str,
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
) -> dict[str, Any]:
    """回查一轮问数: 实际执行的 SQL、本轮 DSL 与槽位(按需拉取)。

    为什么 SQL 走单独接口而不是塞进对话载荷: 它是排障视图, 只在有人点开时取一次;
    SSE 与会话历史里每轮都带一段 SQL 既费流量又和“答案正文”无关。普通角色只能看
    自己问过的那一轮, 能跨全员看数的角色不受限(与 ``/queries`` 同一口径)。
    """
    row = await run_in_threadpool(store.get_query, query_id)
    if not row:
        raise HTTPException(status_code=404, detail="没找到这条问数记录")
    caller = (operator or "").strip()
    if not _privileged(role) and str(row.get("user_id") or "") != caller:
        raise HTTPException(status_code=403, detail="只能查看自己问过的 SQL")
    return _query_detail(row)


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
    # Loki 是可选副本(默认不推): 推的是治理信号而不是数据内容，失败不影响本轮反馈已落库。
    logsink.push_async("feedback", {
        "query_id": req.query_id, "kind": req.kind, "category": category,
        "disposition": disposition, "sample": str(routed.get("sample") or ""),
        "route": str(row.get("route") or ""), "verdict": str(row.get("verdict") or ""),
        "semver": str(row.get("semver") or ""),
    }, labels={"user_id": caller or "anonymous"})
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


@router.post("/events")
async def submit_event(req: EventIn) -> dict[str, Any]:
    """收一条结构化反馈事件(edit_query/edit_dsl/copy/drill/...), 并尽量归因。

    与 ``/feedback`` 的区别不是历史包袋: feedback 只能表达"好/不好", 而这里要拿
    "用户到底改了什么"当证据 —— edit_dsl 的 diff 能把责任直接定到某个字段。
    普通角色只能给自己的问数轮记事件(与 /feedback 同一口径)。
    """
    if req.event_type not in store.EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"event_type 只允许 {list(store.EVENT_TYPES)}")
    row = {}
    if req.query_id:
        row = await run_in_threadpool(store.get_query, req.query_id)
        if not row:
            raise HTTPException(status_code=404, detail="没找到这条问数记录")
        caller = (req.user_id or "").strip()
        if caller and str(row.get("user_id") or "") not in (caller, ""):
            raise HTTPException(status_code=403, detail="只能对自己问过的记录给反馈")
    event_id = await run_in_threadpool(
        store.record_event,
        query_id=req.query_id, trace_id=req.trace_id or str(row.get("trace_id") or ""),
        session_id=req.session_id or str(row.get("session_id") or ""),
        user_id=req.user_id or str(row.get("user_id") or ""),
        event_type=req.event_type, payload=req.payload or {}, stage=req.stage,
    )
    out: dict[str, Any] = {"status": "ok" if event_id else "degraded", "event_id": event_id}
    corrected = (req.payload or {}).get("dsl") if req.event_type == "edit_dsl" else None
    # 事件类型 -> feedback 的映射只在这一处: copy/drill 是中性行为信号(拿走了数据/想看明细),
    # 不能当成"满意"也不能当成"不满", 否则点踩率会被一堆无关点击拉低。
    kind = {"like": "up", "dislike": "down", "noanswer": "down"}.get(req.event_type, "")
    if row and kind and str(req.query_id or ""):
        await run_in_threadpool(store.set_feedback, req.query_id, kind, user_id=req.user_id or "")
    if row and corrected and req.query_id:
        await run_in_threadpool(
            store.set_feedback, req.query_id, "rewritten",
            corrected_dsl=corrected, note="由 edit_dsl 事件产生", user_id=req.user_id or "",
        )
    if row and req.attribute and (req.query_id or kind or corrected):
        signal = "rewritten" if corrected else (kind or req.event_type)
        category, disposition = attribution.attribute(
            {**row, "feedback": signal, "corrected_dsl": corrected}
        )
        routed = await run_in_threadpool(
            attribution.apply_disposition,
            {**row, "feedback": signal},
            category=category, disposition=disposition, corrected_dsl=corrected,
        )
        out.update({
            "category": category, "disposition": disposition,
            "draft": routed.get("written", ""),
            "diff": {k: [v[0], v[1]] for k, v in attribution.dsl_diff(
                row.get("dsl_json"), corrected).items()},
        })
    _audit.log(req.trace_id or str(row.get("trace_id") or ""), "assistant", "askdata_event",
               {"query_id": req.query_id, "event_type": req.event_type,
                "user_id": req.user_id or "anonymous", "event_id": event_id})
    logsink.push_async("event", {
        "query_id": req.query_id, "event_type": req.event_type,
        "category": str(out.get("category") or ""),
        "disposition": str(out.get("disposition") or ""),
        "diff_keys": ",".join(sorted((out.get("diff") or {}).keys())),
        "semver": str(row.get("semver") or ""),
    }, labels={"user_id": req.user_id or "anonymous"})
    return out


@router.get("/events")
async def list_events(
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
    query_id: str = Query(default="", description="按那一轮回查"),
    event_type: str = Query(default="", description="按事件类型过滤"),
    days: int = Query(default=30, ge=1, le=90),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """事件列表(治理面): 看"哪类事件最多"来定下一个该修什么。"""
    _require_privileged(role, operator, "反馈事件")
    return {"events": await run_in_threadpool(
        store.list_events, query_id=query_id, event_type=event_type, days=days, limit=limit
    )}


@router.post("/tickets")
async def open_ticket(req: TicketIn, role: str = Query(default="employee")) -> dict[str, Any]:
    """开一张人工工单(页面上的"这个问题要人看"按钮)。

    状态机在超预算/引擎熔断时会自己开(走 MCP 工具), 这里给的是"用户主动要人"的入口。
    """
    if req.reason not in store.TICKET_REASONS:
        raise HTTPException(status_code=400, detail=f"reason 只允许 {list(store.TICKET_REASONS)}")
    out = await run_in_threadpool(
        store.create_ticket,
        question=req.question, reason=req.reason, trace_id=req.trace_id, query_id=req.query_id,
        dsl=req.dsl, semver=req.semver, requested_by=req.requested_by,
    )
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out["error"])
    _audit.log(req.trace_id, "assistant", "askdata_ticket", {**out, "role": role})
    return out


@router.get("/tickets")
async def get_tickets(
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
    status: str = Query(default="open", description="open/processing/resolved/rejected, 空串=全部"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """工单列表(只有治理角色能看全量; 普通角色的自己开的单暂时不单独开接口)。"""
    _require_privileged(role, operator, "工单列表")
    return {"tickets": await run_in_threadpool(store.list_tickets, status=status, limit=limit)}


@router.post("/tickets/{ticket_id}")
async def patch_ticket(
    ticket_id: str, req: TicketIn, role: str = Query(default="employee"),
) -> dict[str, Any]:
    """推进一张工单(处理人/状态/结论): 治理动作, 需要特权角色。"""
    _require_privileged(role, req.assigned_to, "工单处理")
    out = await run_in_threadpool(
        store.update_ticket, ticket_id, status=req.status or "processing",
        resolution=req.resolution, assigned_to=req.assigned_to,
    )
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out["error"])
    return out


@router.get("/alerts")
async def alert_lines(
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    """告警线: 执行错误率/澄清率突增/降级率/EX 掉分 四条规则现在是否越线。"""
    _require_privileged(role, operator, "告警线")
    from app.observability import alerts

    stats = await run_in_threadpool(store.stats, days=days)
    return {"window_days": days, "alerts": alerts.evaluate(stats), "stats": stats}


@router.post("/golden")
async def add_golden(req: GoldenIn, role: str = Query(default="employee")) -> dict[str, Any]:
    """新增/更新一条评测用例(草案应由归因路由产生, 这里给页面/脚本手工补录用)。"""
    _require_privileged(role, req.created_by, "评测集写入")
    out = await run_in_threadpool(
        store.upsert_golden,
        question=req.question, dsl=req.dsl, expected_sql=req.expected_sql,
        expected_result=req.expected_result, tags=req.tags, note=req.note,
        created_by=req.created_by, enabled=req.enabled, case_id=req.case_id,
        slots=req.slots, difficulty=req.difficulty, intent=req.intent,
    )
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out["error"])
    _audit.log("", "assistant", "askdata_golden_upsert",
               {"case_id": out.get("case_id"), "enabled": out.get("enabled"),
                "operator": req.created_by or "anonymous"})
    return out


@router.post("/golden/{case_id}/cacheable")
async def approve_golden_cache(
    case_id: str,
    cacheable: bool = Query(description="true=允许这条问法被语义缓存复用"),
    role: str = Query(default="employee"),
    operator: str = Query(default=""),
) -> dict[str, Any]:
    """批准/收回某条问法参与语义缓存(治理动作)。

    为什么默认全部不可复用: 缓存一旦命中就是"拿 A 的口径回答 B"。
    golden 里批量生成的合成问法只当尺子, 要拿它服务真实用户必须逐条批准。
    """
    _require_privileged(role, operator, "语义缓存批准")
    out = await run_in_threadpool(store.set_golden_cacheable, case_id, cacheable=cacheable)
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out["error"])
    _audit.log("", "assistant", "askdata_golden_cacheable",
               {**out, "operator": operator, "role": role})
    return out


@router.get("/samples")
async def list_samples(
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
    status: str = Query(default="draft", description="draft/approved/rejected/exported, 空=全部"),
    kind: str = Query(default="", description="sft / dpo"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """SFT/DPO 样本队列(只给治理角色): 看"哪些轮次可以当训练材料"。

    队列里的每一行都带着两道门禁的结论, 所以"为什么不导出这条"能在数据里看到,
    不用人去读 note 猜。
    """
    _require_privileged(role, operator, "训练样本队列")
    return {
        "samples": await run_in_threadpool(
            store.list_samples, status=status, kind=kind, limit=limit
        ),
        "stats": await run_in_threadpool(store.sample_stats),
        "trainable_categories": list(store.TRAINABLE_CATEGORIES),
        "gate_strict": bool(get_settings().askdata_sample_gate_strict),
    }


@router.post("/samples/{sample_id}")
async def review_sample_endpoint(
    sample_id: str,
    status: str = Query(description="approved / rejected / draft"),
    role: str = Query(default="employee"),
    operator: str = Query(default=""),
    note: str = Query(default=""),
) -> dict[str, Any]:
    """人工复核一条样本(批准/驳回)。导出脚本只读 approved 且两道门禁都过的。"""
    _require_privileged(role, operator, "样本复核")
    out = await run_in_threadpool(
        store.review_sample, sample_id, status=status, reviewer=operator, note=note,
    )
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out["error"])
    _audit.log("", "assistant", "askdata_sample_review",
               {**out, "role": role, "operator": operator})
    return out


class SampleIn(BaseModel):
    """手工补录一条训练样本(治理面用; 自动产生走归因路由)。"""

    kind: str = "sft"
    question: str
    chosen: dict[str, Any]
    rejected: dict[str, Any] | None = None
    query_id: str = ""
    semver: str = ""
    category: str = "dsl"
    note: str = ""
    reviewer: str = ""


@router.post("/samples")
async def add_sample(req: SampleIn, role: str = Query(default="employee")) -> dict[str, Any]:
    """补录样本。chosen 必须真能过校验+编译(不靠人口头保证)。"""
    _require_privileged(role, req.reviewer, "训练样本写入")
    from app.askdata.attribution import _dsl_is_executable

    executable = await run_in_threadpool(_dsl_is_executable, req.chosen)
    out = await run_in_threadpool(
        store.record_sample,
        kind=req.kind, question=req.question, chosen=req.chosen, rejected=req.rejected,
        query_id=req.query_id, semver=req.semver, category=req.category or "dsl",
        user_id=req.reviewer, gate_validated=executable, gate_confirmed=True,
        note=req.note or "治理面手工补录",
    )
    if out.get("error"):
        raise HTTPException(status_code=400, detail=out["error"])
    out["gate_validated"] = executable
    return out


@router.get("/semantic")
async def semantic_snapshot(
    role: str = Query(default="employee", description="调用者角色"),
    operator: str = Query(default="", description="调用者工号"),
    refresh: bool = Query(default=False, description="强制重读语义层快照"),
) -> dict[str, Any]:
    """语义层快照概览(排查用): 现在用的是库里版本还是内置兜底集, 以及各细分面的情况。"""
    _require_privileged(role, operator, "语义层快照")
    from app.semantic import registry

    if refresh:
        registry.invalidate()
    catalog = await run_in_threadpool(registry.get_catalog)
    changes = await run_in_threadpool(_recent_changes)
    return {
        "catalog": catalog.to_payload(),
        "semver": catalog.semver,
        "object_versions": catalog.object_versions(),
        "recent_changes": changes,
        "golden_enabled": await run_in_threadpool(store.golden_count),
        "golden_min_required": int(get_settings().askdata_golden_min_cases or 0),
        "metrics": catalog.render_metrics(),
        "dimensions": catalog.render_dimensions(),
    }


def _recent_changes(limit: int = 20) -> list[dict[str, Any]]:
    """最近的语义层版本记录(看"口径是谁什么时候改的")。"""
    from sqlalchemy import text

    from app.db import sync as dbsync

    sql = (
        "SELECT semver, object_kind, object_id, version, actor, source, note, created_at "
        "FROM sem_change_log ORDER BY created_at DESC LIMIT :limit"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), {"limit": max(1, min(100, limit))}).all()
    except Exception:  # noqa: BLE001 - 版本记录读不出不影响快照本身(存量库可能还没这张表)
        return []
    return [dict(r._mapping) for r in rows]


# 详情返回的列: 刻意不含 candidates_json / metric_summary。前者是召回候选的长文本,
# 后者是结果量级摘要, 都不回答“这句话最后编成了什么 SQL”。
_QUERY_DETAIL_KEYS = (
    "query_id", "trace_id", "session_id", "user_id", "role", "question",
    "route", "verdict", "error_codes", "rowcount", "latency_ms", "token_cost",
    "semver", "catalog_origin", "cache_hit", "feedback", "attribution", "notes",
    "final_sql", "dsl_json", "slots_json", "created_at",
)


def _query_detail(row: dict[str, Any]) -> dict[str, Any]:
    """裁出排障需要的列并统一空值形状(错误码永远是数组, 前端不用判 null)。"""
    out = {key: row.get(key) for key in _QUERY_DETAIL_KEYS if key in row}
    out["error_codes"] = list(out.get("error_codes") or [])
    return out


def _require_privileged(role: str, operator: str, what: str) -> None:
    """只判角色; ``operator`` 存在的意义是审计里能写下"谁看了这一页"(写不写入由调用方定)。"""
    _ = (operator or "").strip()
    if not _privileged(role):
        raise HTTPException(status_code=403, detail=f"角色 {role or 'employee'} 无权查看{what}")
