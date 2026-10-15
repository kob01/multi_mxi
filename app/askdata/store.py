"""问数留痕 / 反馈 / 评测集 / 评测结论的同步 DAO。

同步而不是异步有两个理由, 都不是偏好:
1. askdata MCP server 的工具是同步函数(FastMCP 把它们放到 threadpool 里跑),
   留痕必须能在同一个调用栈里写完, 不能"异步工具才能留痕";
2. 网关 REST 复用同一份实现(照 ``app/dataops/router.py`` 的做法用
   ``run_in_threadpool`` 包一层), 于是不存在"两条路径写出两种形状"的风险。

所有写路径都是**尽力而为**: 留痕表写不进去只意味着看板少一条, 绝不能把已经算出来的
答案丢掉(那才是用户能感知的故障)。这一点在返回体里明说(``status=degraded``), 而不是
静默返空 —— 静默失败的观测面本身就是最需要被看见的失败。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app.db import sync as dbsync
from app.semantic.resultcache import SEMANTIC_CACHE_MARKER

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

VERDICTS = ("ok", "clarify", "denied", "fail", "degraded")
ROUTES = ("metric_api", "compiler", "sandbox", "clarify", "repair", "degrade")
FEEDBACKS = ("up", "down", "rewritten")
ATTRIBUTIONS = ("intent", "link", "dsl", "exec", "render")
# 结构化反馈事件(方案 §4⑥): 比 FEEDBACKS 那三个枚举多出"用户改了什么"这个证据面。
EVENT_TYPES = (
    "like", "dislike", "edit_query", "edit_dsl", "copy", "drill",
    "clarify_pick", "ticket", "noanswer",
)
TICKET_REASONS = (
    "no_metric", "no_data", "budget_exceeded", "engine_error", "user_request", "bad_answer",
)

_INSERT = """
INSERT INTO askdata_query_log (
    query_id, trace_id, session_id, tenant_id, user_id, role, question,
    slots_json, candidates_json, dsl_json, route, final_sql, rowcount,
    latency_ms, token_cost, llm_calls, retry_count, semver, catalog_origin, cache_hit,
    metric_summary, verdict, error_codes, notes, feedback, attribution
) VALUES (
    :query_id, :trace_id, :session_id, :tenant_id, :user_id, :role, :question,
    :slots, :candidates, :dsl, :route, :final_sql, :rowcount,
    :latency_ms, :token_cost, :llm_calls, :retry_count, :semver, :catalog_origin, :cache_hit,
    :metric_summary, :verdict, :error_codes, :notes, '', ''
)
"""


def _json(value: Any) -> str:
    import json

    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False, default=str)


def record_query(payload: dict[str, Any]) -> str:
    """写一条问数留痕, 返回 query_id; 写失败返回空串(调用方据此标降级)。"""
    query_id = uuid.uuid4().hex
    row = {
        "query_id": query_id,
        "trace_id": str(payload.get("trace_id") or "")[:64],
        "session_id": str(payload.get("session_id") or "")[:64],
        "tenant_id": str(payload.get("tenant_id") or "")[:32],
        "user_id": str(payload.get("user_id") or "")[:64],
        "role": str(payload.get("role") or "")[:16],
        "question": str(payload.get("question") or "")[:4000],
        "slots": _json(payload.get("slots") or {}),
        "candidates": _json(payload.get("candidates") or []),
        "dsl": _json(payload.get("dsl") or {}),
        "route": str(payload.get("route") or "")[:24],
        "final_sql": str(payload.get("final_sql") or "")[:4000],
        "rowcount": int(payload.get("rowcount") or 0),
        "latency_ms": int(payload.get("latency_ms") or 0),
        "token_cost": int(payload.get("token_cost") or 0),
        "llm_calls": int(payload.get("llm_calls") or 0),
        "retry_count": int(payload.get("retry_count") or 0),
        "semver": str(payload.get("semver") or "")[:32],
        "catalog_origin": str(payload.get("catalog_origin") or "")[:16],
        "cache_hit": bool(payload.get("cache_hit") or False),
        "metric_summary": _json(payload.get("metric_summary") or None) if payload.get("metric_summary") else None,
        "verdict": str(payload.get("verdict") or "ok")[:16],
        "error_codes": _json(payload.get("error_codes") or []),
        "notes": str(payload.get("notes") or "")[:2000],
    }
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(text(_INSERT), row)
            # 澄清点选也是一条事件(误澄清率的原料): 上一轮问了、但用户选的正是最高分候选
            # —— 那一句本可以不问。不记它就只算得出"问了几个", 算不出"问错了几个"。
            picked = str(payload.get("clarify_pick") or "")
            if picked:
                record_event(
                    query_id=query_id, trace_id=row["trace_id"], session_id=row["session_id"],
                    user_id=row["user_id"], event_type="clarify_pick",
                    payload={
                        "chosen": picked,
                        "top": str(payload.get("clarify_top") or ""),
                        "matched": picked == str(payload.get("clarify_top") or ""),
                    },
                    conn=conn,
                )
    except Exception as exc:  # noqa: BLE001 - 留痕失败不阻断回答, 但要把失败暴露出来
        logger.warning("[askdata] 问数留痕写入失败(本轮看板将缺这一条): %s", exc)
        return ""
    return query_id


def set_feedback(
    query_id: str, kind: str, *, attribution: str = "", corrected_dsl: Any = None,
    note: str = "", user_id: str = "",
) -> dict[str, Any]:
    """记录用户反馈(点赞/点踩/改写)并落归因类别。

    只允许改写**自己**问过的那条: 反馈本身是训练语料与语义层修订的输入, 让别人替你的
    提问下结论会让归因数据失去意义(与 ``list_my_dataops`` 同一口径)。
    """
    if kind not in FEEDBACKS:
        return {"error": f"feedback 只允许 {list(FEEDBACKS)}, 收到 {kind!r}"}
    sql = (
        "UPDATE askdata_query_log SET feedback = :kind, attribution = :attribution, "
        "notes = CASE WHEN :note = '' THEN notes ELSE notes || E'\\n' || :note END "
        "WHERE query_id = :query_id"
    )
    if user_id:
        sql += " AND user_id = :user_id"
    params = {
        "kind": kind, "attribution": (attribution or "")[:16], "note": (note or "")[:1000],
        "query_id": (query_id or "")[:64], "user_id": (user_id or "")[:64],
    }
    try:
        with dbsync.get_sync_engine().begin() as conn:
            result = conn.execute(text(sql), params)
            updated = int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 反馈写入失败: %s", exc)
        return {"error": f"反馈写入失败: {type(exc).__name__}"}
    if not updated:
        return {"error": "没找到这条问数记录(或不属于该用户)", "query_id": query_id}
    if corrected_dsl:
        _append_corrected_dsl(query_id, corrected_dsl)
    return {"status": "ok", "query_id": query_id, "feedback": kind,
            "attribution": attribution or "", "updated": updated}


def _append_corrected_dsl(query_id: str, corrected_dsl: Any) -> None:
    """把用户改写后的 DSL 追加到 notes(它是评测集补录与语义层修订的证据)。"""
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(
                text("UPDATE askdata_query_log SET notes = notes || E'\\n[改写 DSL] ' || :d "
                     "WHERE query_id = :id"),
                {"d": _json(corrected_dsl)[:1500], "id": query_id[:64]},
            )
    except Exception as exc:  # noqa: BLE001 - 证据追加失败不影响反馈本身已落库
        logger.warning("[askdata] 改写 DSL 追加失败: %s", exc)


def get_query(query_id: str) -> dict[str, Any]:
    with dbsync.get_sync_engine().connect() as conn:
        row = conn.execute(text("SELECT * FROM askdata_query_log WHERE query_id = :id"),
                           {"id": query_id[:64]}).first()
    return dict(row._mapping) if row else {}


# ---------------------------------------------------------------------------
# 结构化反馈事件与人工工单
# ---------------------------------------------------------------------------
def record_event(
    *, query_id: str = "", trace_id: str = "", session_id: str = "", user_id: str = "",
    event_type: str, payload: Any = None, stage: str = "", conn=None,
) -> str:
    """写一条反馈事件(复用外部 conn 时不自己开事务: clarify_pick 要跟留痕同一条事务)。

    事件与留痕分两张表而不是给 query_log 加一堆列: 同一轮可以有多个事件
    (既点踩又改了 DSL), 一行一个枚举字段存不下这种共现。
    """
    if event_type not in EVENT_TYPES:
        return ""
    event_id = uuid.uuid4().hex
    sql = (
        "INSERT INTO askdata_feedback_event (event_id, trace_id, query_id, session_id, user_id, "
        "event_type, payload_json, stage) VALUES "
        "(:event_id, :trace_id, :query_id, :session_id, :user_id, :event_type, :payload, :stage)"
    )
    row = {
        "event_id": event_id, "trace_id": (trace_id or "")[:64], "query_id": (query_id or "")[:64],
        "session_id": (session_id or "")[:64], "user_id": (user_id or "")[:64],
        "event_type": event_type[:24], "payload": _json(payload or {}), "stage": (stage or "")[:16],
    }
    try:
        if conn is not None:
            conn.execute(text(sql), row)
        else:
            with dbsync.get_sync_engine().begin() as own:
                own.execute(text(sql), row)
    except Exception as exc:  # noqa: BLE001 - 事件写不进只意味着这一条信号丢了
        logger.warning("[askdata] 反馈事件写入失败(%s): %s", event_type, exc)
        return ""
    return event_id


def list_events(*, query_id: str = "", user_id: str = "", event_type: str = "",
                days: int = 30, limit: int = 200) -> list[dict[str, Any]]:
    where = ["created_at >= :since"]
    params: dict[str, Any] = {
        "since": (datetime.now(_CST) - timedelta(days=max(1, days))).isoformat(),
        "limit": max(1, min(500, int(limit or 200))),
    }
    for name, value in (("query_id", query_id), ("user_id", user_id), ("event_type", event_type)):
        if value:
            where.append(f"{name} = :{name}")
            params[name] = value
    sql = (
        f"SELECT * FROM askdata_feedback_event WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC LIMIT :limit"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), params).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 反馈事件回查失败: %s", exc)
        return []
    return [dict(r._mapping) for r in rows]


def create_ticket(
    *, question: str, reason: str = "user_request", trace_id: str = "", query_id: str = "",
    dsl: Any = None, semver: str = "", requested_by: str = "", assigned_to: str = "",
) -> dict[str, Any]:
    """开一张人工工单(FALLBACK 链的最后一出口)。

    为什么这是必须的一步而不是"建议用户去找 IT": 问数的降级链只能给"建议问法",
    但有一类问题就是该由人看(口径不存在 / 源头数据缺 / 用户不信这个数)。没有出口
    就是系统默默把球掉在地上。
    """
    if reason not in TICKET_REASONS:
        return {"error": f"reason 只允许 {list(TICKET_REASONS)}, 收到 {reason!r}"}
    ticket_id = uuid.uuid4().hex[:24]
    sql = (
        "INSERT INTO askdata_ticket (ticket_id, trace_id, query_id, question, reason, dsl_json, "
        "semver, requested_by, assigned_to, status, resolution) VALUES (:ticket_id, :trace_id, "
        ":query_id, :question, :reason, :dsl, :semver, :requested_by, :assigned_to, 'open', '')"
    )
    row = {
        "ticket_id": ticket_id, "trace_id": (trace_id or "")[:64], "query_id": (query_id or "")[:64],
        "question": (question or "")[:4000], "reason": reason[:32], "dsl": _json(dsl or {}),
        "semver": (semver or "")[:32], "requested_by": (requested_by or "")[:64],
        "assigned_to": (assigned_to or "")[:64],
    }
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(text(sql), row)
            record_event(query_id=query_id, trace_id=trace_id, user_id=requested_by,
                         event_type="ticket", payload={"ticket_id": ticket_id, "reason": reason},
                         conn=conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 工单写入失败: %s", exc)
        return {"error": f"工单写入失败: {type(exc).__name__}"}
    return {"status": "ok", "ticket_id": ticket_id}


def list_tickets(*, status: str = "open", limit: int = 50) -> list[dict[str, Any]]:
    where = "WHERE status = :status" if status else ""
    sql = f"SELECT * FROM askdata_ticket {where} ORDER BY created_at DESC LIMIT :limit"
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), {
                "status": status[:16], "limit": max(1, min(200, int(limit or 50))),
            }).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 工单列表读取失败: %s", exc)
        return []
    return [dict(r._mapping) for r in rows]


def update_ticket(ticket_id: str, *, status: str, resolution: str = "",
                  assigned_to: str = "") -> dict[str, Any]:
    if status not in ("open", "processing", "resolved", "rejected"):
        return {"error": "status 只允许 open/processing/resolved/rejected"}
    sql = (
        "UPDATE askdata_ticket SET status = :status, "
        "resolution = CASE WHEN :resolution = '' THEN resolution ELSE :resolution END, "
        "assigned_to = CASE WHEN :assigned_to = '' THEN assigned_to ELSE :assigned_to END "
        "WHERE ticket_id = :ticket_id"
    )
    try:
        with dbsync.get_sync_engine().begin() as conn:
            result = conn.execute(text(sql), {
                "status": status, "resolution": (resolution or "")[:2000],
                "assigned_to": (assigned_to or "")[:64], "ticket_id": (ticket_id or "")[:64],
            })
            updated = int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 工单更新失败: %s", exc)
        return {"error": f"工单更新失败: {type(exc).__name__}"}
    if not updated:
        return {"error": "没找到这张工单", "ticket_id": ticket_id}
    return {"status": "ok", "ticket_id": ticket_id, "ticket_status": status}


def list_queries(
    *, user_id: str = "", verdict: str = "", feedback: str = "", days: int = 7, limit: int = 50,
) -> list[dict[str, Any]]:
    """回查问数记录(默认只看最近 7 天, 条数有界)。"""
    limit = max(1, min(200, int(limit or 50)))
    where = ["created_at >= :since"]
    params: dict[str, Any] = {"since": (datetime.now(_CST) - timedelta(days=max(1, days))).isoformat(),
                              "limit": limit}
    if user_id:
        where.append("user_id = :user_id")
        params["user_id"] = user_id
    if verdict:
        where.append("verdict = :verdict")
        params["verdict"] = verdict
    if feedback:
        where.append("feedback = :feedback")
        params["feedback"] = feedback
    sql = (
        f"SELECT query_id, trace_id, user_id, role, question, route, verdict, error_codes, "
        f"rowcount, latency_ms, token_cost, semver, catalog_origin, cache_hit, feedback, "
        f"attribution, notes, created_at "
        f"FROM askdata_query_log WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC LIMIT :limit"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), params).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 问数记录回查失败: %s", exc)
        return []
    return [dict(r._mapping) for r in rows]


def stats(*, days: int = 7) -> dict[str, Any]:
    """指标看板的一次性聚合: 通过率/澄清触发率/误澄清率/降级率/缓存命中率/点踩率/P95 + 最近 EX。

    口径写在这里而不是让前端算: 分母是"进入状态机的问数轮次"(含被澄清与被拒的),
    只统计成功轮次的"一次通过率"毫无意义(它永远是 100%)。
    """
    since = (datetime.now(_CST) - timedelta(days=max(1, days))).isoformat()
    # 语义缓存命中靠注记判(它复用的是"意图"不是"数据", 与结果缓存不是一回事):
    # 没有这一项就看不出"语义缓存默默没命中"—— 本轮回踩过一次(text 未导入导致永远不命中,
    # 而准确率与延迟看起来都"很正常")。模式串走绑参而不是写在 SQL 里: 这条语句用的是
    # psycopg 的 %(name)s 风格, SQL 文本里的裸 % 会被当转义符吃掉。
    sem_marker = "%" + SEMANTIC_CACHE_MARKER + "%"
    sql = """
SELECT COUNT(*) AS total,
       COUNT(*) FILTER (WHERE verdict = 'ok' AND route IN ('metric_api', 'compiler', 'sandbox')) AS first_pass,
       COUNT(*) FILTER (WHERE verdict = 'clarify') AS clarified,
       COUNT(*) FILTER (WHERE route IN ('sandbox', 'degrade', 'repair')) AS degraded,
       COUNT(*) FILTER (WHERE verdict IN ('denied', 'fail')) AS failed,
       COUNT(*) FILTER (WHERE verdict = 'degraded') AS fallbacked,
       COUNT(*) FILTER (WHERE cache_hit) AS cache_hits,
       COUNT(*) FILTER (WHERE notes LIKE :sem_marker) AS sem_cache_hits,
       COUNT(*) FILTER (WHERE feedback = 'down') AS thumbs_down,
       COUNT(*) FILTER (WHERE feedback <> '') AS feedbacked,
       COALESCE(SUM(token_cost), 0) AS tokens,
       COALESCE(AVG(latency_ms), 0) AS avg_ms,
       COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), 0) AS p95_ms
FROM askdata_query_log WHERE created_at >= :since
"""
    mis_sql = """
SELECT COUNT(*) FILTER (WHERE payload_json ->> 'matched' = 'true') AS matched,
       COUNT(*) AS picked
FROM askdata_feedback_event
WHERE event_type = 'clarify_pick' AND created_at >= :since
"""
    try:
        with dbsync.get_sync_engine().connect() as conn:
            row = conn.execute(text(sql), {"since": since, "sem_marker": sem_marker}).first()
            mis = conn.execute(text(mis_sql), {"since": since}).first()
            latest = conn.execute(
                text("SELECT run_id, exec_accuracy, dsl_exact_match, slot_f1, mode, case_count, "
                     "gate_status, semver, started_at "
                     "FROM askdata_eval_run ORDER BY started_at DESC LIMIT 1")
            ).first()
            # 评测集规模也算进来: 告警线要判"样本够不够", 让调用方再发一次查询就会
            # 出现"看板一个数、告警另一个数"。
            golden = conn.execute(
                text("SELECT COUNT(*) FROM askdata_golden WHERE enabled")
            ).scalar_one()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 看板聚合失败: %s", exc)
        return {"error": f"看板聚合失败: {type(exc).__name__}", "window_days": days}
    data = dict(row._mapping) if row else {}
    total = int(data.get("total") or 0)
    golden = int(golden or 0)

    def _rate(value: Any, base: int = 0) -> float:
        denominator = base if base else total
        return round(int(value or 0) * 100.0 / denominator, 1) if denominator else 0.0

    out = {
        "window_days": days,
        "total": total,
        "golden_enabled": golden,
        # 已批准参与语义缓存的条数(默认为 0: 合成 golden 不自动获得复用资格)。
        "golden_cacheable": golden_cacheable_count(),
        "first_pass_rate": _rate(data.get("first_pass")),
        "clarify_rate": _rate(data.get("clarified")),
        "degrade_rate": _rate(data.get("degraded")),
        "fallback_rate": _rate(data.get("fallbacked")),
        "cache_hit_rate": _rate(data.get("cache_hits")),
        "sem_cache_hit_rate": _rate(data.get("sem_cache_hits")),
        "fail_rate": _rate(data.get("failed")),
        "thumbs_down_rate": (
            round(int(data.get("thumbs_down") or 0) * 100.0 / int(data.get("feedbacked") or 1), 1)
            if int(data.get("feedbacked") or 0) else 0.0
        ),
        "feedback_rate": _rate(data.get("feedbacked")),
        "token_total": int(data.get("tokens") or 0),
        "avg_latency_ms": int(data.get("avg_ms") or 0),
        "p95_latency_ms": int(data.get("p95_ms") or 0),
    }
    # 误澄清率的分母是"澄清了几次"而不是"问了几次": 它回答的是"这些反问里有多少本可以不问"。
    if mis:
        item = dict(mis._mapping)
        out["mis_clarify_rate"] = _rate(item.get("matched"), base=int(item.get("picked") or 0))
        out["clarify_picked"] = int(item.get("picked") or 0)
    if latest:
        item = dict(latest._mapping)
        out["latest_eval"] = {
            "run_id": item.get("run_id"),
            "exec_accuracy": float(item.get("exec_accuracy") or 0),
            "dsl_exact_match": float(item.get("dsl_exact_match") or 0),
            "slot_f1": float(item.get("slot_f1") or 0),
            "mode": item.get("mode"),
            "case_count": int(item.get("case_count") or 0),
            "gate_status": item.get("gate_status") or "",
            # 评测跑的是哪一版口径: 分数必须与口径版本一起看, 否则"EX 掉了"分不清
            # 是能力回退还是有人改了定义。
            "semver": item.get("semver") or "",
            "started_at": str(item.get("started_at") or ""),
        }
    # 样本队列现状也带上: 治理面要一眼看到"攒了多少可训材料, 多少被门禁拦住了"。
    out["samples"] = sample_stats()
    return out


# 能当训练材料的归因类别: link 走补词、exec 走运维/权限、render 走规则引擎,
# 它们都**不该进训练集**(方案明写: 不是所有 badcase 都进训练)。
TRAINABLE_CATEGORIES = ("intent", "dsl")


def record_sample(
    *, kind: str, question: str, chosen: Any, rejected: Any = None, slots: Any = None,
    query_id: str = "", trace_id: str = "", user_id: str = "", role: str = "",
    semver: str = "", category: str = "", gate_validated: bool = False,
    gate_confirmed: bool = False, note: str = "",
) -> dict[str, Any]:
    """写一条 SFT/DPO 候选样本(永远先写 draft, 生效要人工批准)。

    不满足两道门禁的样本仍写下来(带 gate 列), 但状态会是 draft 且导出脚本不拿它们:
    写而不发比"默默丢弃"好 —— 治理面能看到"为什么这些轮次没进训练"。
    """
    if kind not in ("sft", "dpo"):
        return {"error": f"kind 只允许 sft/dpo, 收到 {kind!r}"}
    if not question or not chosen:
        return {"error": "样本缺问法或 chosen DSL"}
    sample_id = uuid.uuid4().hex[:24]
    sql = (
        "INSERT INTO askdata_sample (sample_id, kind, query_id, trace_id, user_id, role, question, "
        "chosen_json, rejected_json, slots_json, semver, category, gate_validated, gate_confirmed, "
        "status, note) VALUES (:sample_id, :kind, :query_id, :trace_id, :user_id, :role, :question, "
        ":chosen, :rejected, :slots, :semver, :category, :gate_validated, :gate_confirmed, "
        "'draft', :note) ON CONFLICT (sample_id) DO NOTHING"
    )
    row = {
        "sample_id": sample_id, "kind": kind[:16], "query_id": (query_id or "")[:64],
        "trace_id": (trace_id or "")[:64], "user_id": (user_id or "")[:64],
        "role": (role or "")[:16], "question": (question or "")[:4000],
        "chosen": _json(chosen), "rejected": _json(rejected) if rejected else None,
        "slots": _json(slots or {}), "semver": (semver or "")[:32],
        "category": (category or "")[:16], "gate_validated": bool(gate_validated),
        "gate_confirmed": bool(gate_confirmed), "note": (note or "")[:2000],
    }
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(text(sql), row)
    except Exception as exc:  # noqa: BLE001 - 样本写不进去不影响本轮反馈已落库
        logger.warning("[askdata] 训练样本写入失败(%s): %s", kind, exc)
        return {"error": f"样本写入失败: {type(exc).__name__}"}
    return {"status": "ok", "sample_id": sample_id, "kind": kind,
            "gate": bool(gate_validated and gate_confirmed)}


def list_samples(*, status: str = "draft", kind: str = "", limit: int = 100) -> list[dict[str, Any]]:
    where, params = [], {"limit": max(1, min(500, int(limit or 100)))}
    if status:
        where.append("status = :status")
        params["status"] = status[:16]
    if kind:
        where.append("kind = :kind")
        params["kind"] = kind[:16]
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"SELECT * FROM askdata_sample {clause} ORDER BY created_at DESC LIMIT :limit"
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), params).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 样本列表读取失败: %s", exc)
        return []
    return [dict(r._mapping) for r in rows]


def review_sample(sample_id: str, *, status: str, reviewer: str = "",
                  note: str = "") -> dict[str, Any]:
    """人工复核一条样本(批准/驳回)。只有 draft 能被推, 不覆盖已导出的。"""
    if status not in ("approved", "rejected", "draft"):
        return {"error": "status 只允许 approved/rejected/draft"}
    sql = (
        "UPDATE askdata_sample SET status = :status, "
        "note = CASE WHEN :note = '' THEN note ELSE note || ' | ' || :note END "
        "WHERE sample_id = :sid AND status = 'draft'"
    )
    try:
        with dbsync.get_sync_engine().begin() as conn:
            result = conn.execute(text(sql), {
                "status": status[:16], "sid": (sample_id or "")[:64],
                "note": ((reviewer or "reviewer") + ": " + (note or "已人工复核"))[:1900],
            })
            updated = int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 样本复核写入失败: %s", exc)
        return {"error": f"样本复核写入失败: {type(exc).__name__}"}
    if not updated:
        return {"error": "没找到这条草案样本(或它已被复核过)", "sample_id": sample_id}
    return {"status": "ok", "sample_id": sample_id, "sample_status": status}


def exportable_samples(*, strict: bool = True, limit: int = 5000) -> list[dict[str, Any]]:
    """可导出样本: approved 且两道门禁都过(strict=True 时)。"""
    gate = "AND gate_validated = true AND gate_confirmed = true" if strict else ""
    sql = (
        f"SELECT * FROM askdata_sample WHERE status = 'approved' {gate} "
        "ORDER BY created_at LIMIT :limit"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), {"limit": max(1, min(20000, int(limit)))}).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 可导出样本读取失败: %s", exc)
        return []
    return [dict(r._mapping) for r in rows]


def mark_samples_exported(ids: list[str]) -> int:
    if not ids:
        return 0
    sql = "UPDATE askdata_sample SET status = 'exported' WHERE sample_id = ANY(:ids)"
    try:
        with dbsync.get_sync_engine().begin() as conn:
            result = conn.execute(text(sql), {"ids": [str(i)[:64] for i in ids]})
            return int(result.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 样本标记已导出失败: %s", exc)
        return 0


def sample_stats() -> dict[str, int]:
    """队列现状(看板与导出脚本都要看): 按状态与"门禁未过多少条"。"""
    sql = (
        "SELECT COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE status = 'draft') AS draft, "
        "COUNT(*) FILTER (WHERE status = 'approved') AS approved, "
        "COUNT(*) FILTER (WHERE status = 'exported') AS exported, "
        "COUNT(*) FILTER (WHERE NOT (gate_validated AND gate_confirmed)) AS gated_out "
        "FROM askdata_sample"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            row = conn.execute(text(sql)).first()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 样本统计失败: %s", exc)
        return {}
    data = dict(row._mapping) if row else {}
    return {key: int(value or 0) for key, value in data.items()}


def record_eval_run(summary: dict[str, Any]) -> str:
    """落一条评测结论(由 ``scripts/askdata_eval`` 调用; 逐条明细在报告文件里)。"""
    run_id = str(summary.get("run_id") or uuid.uuid4().hex)[:64]
    sql = """
INSERT INTO askdata_eval_run (run_id, started_at, case_count, exec_accuracy, dsl_exact_match,
    slot_f1, first_pass_rate, clarify_rate, mis_clarify_rate, degrade_rate, fallback_rate,
    p95_latency_ms, gate_status, gate_note, baseline_run_id, semver, mode, report_path,
    summary_json, created_by)
VALUES (:run_id, :started_at, :case_count, :exec_accuracy, :dsl_exact_match,
    :slot_f1, :first_pass_rate, :clarify_rate, :mis_clarify_rate, :degrade_rate, :fallback_rate,
    :p95_latency_ms, :gate_status, :gate_note, :baseline_run_id, :semver, :mode, :report_path,
    :summary, :created_by)
ON CONFLICT (run_id) DO UPDATE SET
    case_count = EXCLUDED.case_count, exec_accuracy = EXCLUDED.exec_accuracy,
    dsl_exact_match = EXCLUDED.dsl_exact_match, slot_f1 = EXCLUDED.slot_f1,
    first_pass_rate = EXCLUDED.first_pass_rate, clarify_rate = EXCLUDED.clarify_rate,
    mis_clarify_rate = EXCLUDED.mis_clarify_rate, degrade_rate = EXCLUDED.degrade_rate,
    fallback_rate = EXCLUDED.fallback_rate, p95_latency_ms = EXCLUDED.p95_latency_ms,
    gate_status = EXCLUDED.gate_status, gate_note = EXCLUDED.gate_note,
    baseline_run_id = EXCLUDED.baseline_run_id, semver = EXCLUDED.semver,
    mode = EXCLUDED.mode, report_path = EXCLUDED.report_path,
    summary_json = EXCLUDED.summary_json
"""
    row = {
        "run_id": run_id,
        "started_at": summary.get("started_at") or datetime.now(_CST),
        "case_count": int(summary.get("case_count") or 0),
        "exec_accuracy": float(summary.get("exec_accuracy") or 0),
        "dsl_exact_match": float(summary.get("dsl_exact_match") or 0),
        "slot_f1": float(summary.get("slot_f1") or 0),
        "first_pass_rate": float(summary.get("first_pass_rate") or 0),
        "clarify_rate": float(summary.get("clarify_rate") or 0),
        "mis_clarify_rate": float(summary.get("mis_clarify_rate") or 0),
        "degrade_rate": float(summary.get("degrade_rate") or 0),
        "fallback_rate": float(summary.get("fallback_rate") or 0),
        "p95_latency_ms": int(summary.get("p95_latency_ms") or 0),
        "gate_status": str(summary.get("gate_status") or "")[:16],
        "gate_note": str(summary.get("gate_note") or "")[:256],
        "baseline_run_id": str(summary.get("baseline_run_id") or "")[:64],
        "semver": str(summary.get("semver") or "")[:32],
        "mode": str(summary.get("mode") or "full")[:32],
        "report_path": str(summary.get("report_path") or "")[:512],
        "summary": _json(summary.get("detail") or {}),
        "created_by": str(summary.get("created_by") or "")[:64],
    }
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(text(sql), row)
    except Exception as exc:  # noqa: BLE001 - 评测结论落库失败不毁掉已生成的报告
        logger.warning("[askdata] 评测结论落库失败(报告文件仍在): %s", exc)
        return ""
    return run_id


def latest_eval_run(mode: str = "") -> dict[str, Any]:
    """最近一条(可限定模式)的评测结论: CI 门禁拿它当基线比 EX 降幅。"""
    where, params = ("WHERE mode = :mode", {"mode": mode[:32]}) if mode else ("", {})
    sql = f"SELECT * FROM askdata_eval_run {where} ORDER BY started_at DESC LIMIT 1"
    try:
        with dbsync.get_sync_engine().connect() as conn:
            row = conn.execute(text(sql), params).first()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 基线评测结论读取失败: %s", exc)
        return {}
    return dict(row._mapping) if row else {}


def list_golden(*, enabled_only: bool = True, limit: int = 500) -> list[dict[str, Any]]:
    """读 golden 评测集(启用中的; include_drafts 时连草案一起给)。"""
    where = "WHERE enabled = true" if enabled_only else ""
    sql = (
        "SELECT case_id, question, dsl_json, slots_json, expected_sql, expected_result_json, "
        "tags, difficulty, intent, note, enabled, cacheable, semver, created_by, created_at "
        "FROM askdata_golden "
        f"{where} ORDER BY created_at DESC LIMIT :limit"
    )
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), {"limit": max(1, min(2000, int(limit)))}).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 评测集读取失败: %s", exc)
        return []
    return [dict(r._mapping) for r in rows]


def upsert_golden(
    *, question: str, dsl: Any = None, expected_sql: str = "", expected_result: Any = None,
    tags: str = "", note: str = "", created_by: str = "", enabled: bool = True,
    case_id: str = "", slots: Any = None, difficulty: str = "easy", intent: str = "",
    semver: str = "", cacheable: bool = False,
) -> dict[str, Any]:
    """新增/更新一条 golden 用例(反馈改写来的草案默认 ``enabled=false``, 等人工转正)。

    ``slots`` 与 ``dsl`` 分开存: slot-F1 要的是"应该抽出什么说法", DSL-EM 要的是
    "应该编成什么查询" —— 合在一起存就只能算其中一项。
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.db.models import AskDataGolden

    values = {
        "case_id": case_id or uuid.uuid4().hex[:32],
        "question": (question or "")[:4000],
        "dsl_json": dsl or {},
        "slots_json": slots or {},
        "expected_sql": (expected_sql or "")[:4000],
        "expected_result_json": expected_result or {},
        "tags": (tags or "")[:128],
        "difficulty": (difficulty or "easy")[:16],
        "intent": (intent or "")[:16],
        "note": (note or "")[:2000],
        "semver": (semver or "")[:32],
        "cacheable": bool(cacheable),
        "created_by": (created_by or "")[:64],
        "enabled": bool(enabled),
    }
    stmt = (
        pg_insert(AskDataGolden.__table__).values(**values)
        .on_conflict_do_update(
            index_elements=["case_id"],
            set_={k: values[k] for k in ("question", "dsl_json", "slots_json", "expected_sql",
                                        "expected_result_json", "tags", "difficulty", "intent",
                                        "note", "semver", "cacheable", "enabled")},
        )
    )
    try:
        with dbsync.get_sync_engine().begin() as conn:
            conn.execute(stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 评测集写入失败: %s", exc)
        return {"error": f"评测集写入失败: {type(exc).__name__}"}
    return {"status": "ok", "case_id": values["case_id"], "enabled": values["enabled"]}


def set_golden_cacheable(case_id: str, *, cacheable: bool) -> dict[str, Any]:
    """批准/收回某条问法参与语义缓存(治理动作, 必须有人签名)。

    为什么需要一个单独接口而不是"整批打开": 语义缓存一旦命中就是"拿 A 的口径回答 B",
    只有逐条确认过"这确实是标准答案"的问法才有资格被复用。
    """
    sql = ("UPDATE askdata_golden SET cacheable = :cacheable WHERE case_id = :cid "
           "AND enabled = true RETURNING case_id")
    try:
        with dbsync.get_sync_engine().begin() as conn:
            row = conn.execute(text(sql), {
                "cacheable": bool(cacheable), "cid": (case_id or "")[:64],
            }).first()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 缓存批准写入失败: %s", exc)
        return {"error": f"缓存批准写入失败: {type(exc).__name__}"}
    if row is None:
        return {"error": "没找到这条启用的用例", "case_id": case_id}
    return {"status": "ok", "case_id": case_id, "cacheable": bool(cacheable)}


def golden_without_embedding(limit: int = 5000) -> list[dict[str, Any]]:
    """还没回填向量的启用用例(语义缓存回填脚本的输入)。"""
    sql = ("SELECT case_id, question FROM askdata_golden "
           "WHERE enabled = true AND embedding IS NULL ORDER BY case_id LIMIT :limit")
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), {"limit": max(1, min(20000, int(limit)))}).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[askdata] 待回填向量用例读取失败: %s", exc)
        return []
    return [dict(r._mapping) for r in rows]


def golden_count() -> int:
    try:
        with dbsync.get_sync_engine().connect() as conn:
            return int(conn.execute(text("SELECT COUNT(*) FROM askdata_golden WHERE enabled")).scalar_one() or 0)
    except Exception:  # noqa: BLE001
        return 0


def golden_cacheable_count() -> int:
    """已批准参与语义缓存的问法条数(治理面要知道"缓存现在能服务多少种问法")。"""
    try:
        with dbsync.get_sync_engine().connect() as conn:
            return int(conn.execute(text(
                "SELECT COUNT(*) FROM askdata_golden WHERE enabled AND cacheable"
            )).scalar_one() or 0)
    except Exception:  # noqa: BLE001
        return 0


# 能跨人取基线的角色: 其余角色只能拿自己问过的数做基线 —— 否则"研发部上个月花了多少"
# 会通过基线接口漏给另一个部门(基线也是一次读数)。
_BASELINE_OPEN_ROLES = frozenset({"admin", "finance"})


def metric_baseline(
    *, metric_id: str, shape: str = "", user_id: str = "", role: str = "", limit: int = 20,
) -> dict[str, Any]:
    """同一个口径+同一个形状的历史量级: 中位数 + MAD(稳健, 不被一个错数带跑)。"""
    if not metric_id:
        return {"samples": 0, "reason": "没给 metric_id"}
    scoped = role in _BASELINE_OPEN_ROLES
    sql = """
SELECT metric_summary -> 'metrics' ->> :metric_id AS v
FROM askdata_query_log
WHERE verdict = 'ok' AND metric_summary IS NOT NULL
  AND (:shape = '' OR COALESCE(metric_summary ->> 'shape', '') = :shape)
  AND (:scoped = true OR user_id = :user_id)
ORDER BY created_at DESC LIMIT :limit
"""
    try:
        with dbsync.get_sync_engine().connect() as conn:
            rows = conn.execute(text(sql), {
                "metric_id": metric_id[:64], "shape": (shape or "")[:128],
                "scoped": scoped, "user_id": (user_id or "")[:64],
                "limit": max(5, min(200, int(limit or 20))),
            }).all()
    except Exception as exc:  # noqa: BLE001 - 基线查不到只是少一个参考, 不能卡住回答
        logger.warning("[askdata] 历史基线读取失败: %s", exc)
        return {"samples": 0, "reason": f"基线读取失败({type(exc).__name__})"}
    values: list[float] = []
    for row in rows:
        raw = dict(row._mapping).get("v")
        try:
            values.append(abs(float(raw)))
        except (TypeError, ValueError):
            continue
    if len(values) < 5:
        return {"samples": len(values), "reason": "样本不够 5 条, 不拿它判量级"}
    values.sort()
    median = values[len(values) // 2] if len(values) % 2 else (
        values[len(values) // 2 - 1] + values[len(values) // 2]
    ) / 2.0
    deviations = sorted(abs(v - median) for v in values)
    mad = deviations[len(deviations) // 2]
    return {
        "samples": len(values),
        "median": round(float(median), 4),
        "mad": round(float(mad), 4),
        # MAD 常为 0(同一个口径反复问同一个数), 给一个不使 z-score 发散的地板。
        "scale": round(float(mad) * 1.4826 or max(abs(float(median)) * 0.05, 1.0), 6),
        "scoped": "all" if scoped else "self",
    }
